import asyncio
from typing import List, Optional, Dict, Any, Callable
from client.event_discovery import EventBasedDHTDiscovery, NodeEventListener, NodeEvent, NodeEventType
from client.router import NodeSelector
from common.models import NodeInfo, OpenAIChatCompletionRequest, OpenAICompletionRequest, OpenAIChatCompletionResponse, OpenAICompletionResponse, OpenAIMessage
from common.utils import get_logger
import requests

logger = get_logger(__name__)

class ClientEventListener(NodeEventListener):
    """Client-side event listener for node changes"""
    
    def __init__(self, callback: Optional[Callable] = None):
        self.callback = callback
        self.node_count = 0
    
    async def on_node_event(self, event: NodeEvent):
        """Handle node events"""
        if event.event_type == NodeEventType.NODE_JOINED:
            self.node_count += 1
            logger.info(f"🎉 Client detected new node: {event.node_info.node_id[:12]}... (total: {self.node_count})")
            
        elif event.event_type == NodeEventType.NODE_LEFT:
            self.node_count = max(0, self.node_count - 1)
            logger.info(f"👋 Client detected node departure: {event.node_info.node_id[:12]}... (total: {self.node_count})")
            
        elif event.event_type == NodeEventType.NODE_UPDATED:
            logger.debug(f"🔄 Client detected node update: {event.node_info.node_id[:12]}...")
        
        # Call custom callback if provided
        if self.callback:
            try:
                await self.callback(event)
            except Exception as e:
                logger.error(f"Error in client event callback: {e}")

class EventAwareOpenAIClient:
    """Event-aware OpenAI-compatible client with real-time node discovery"""
    
    def __init__(self, 
                bootstrap_nodes: str = "",
                model: Optional[str] = None,
                min_tps: float = 0.0,
                max_load: float = 1.0,
                dht_port: int = None,
                enable_subnet_filtering: bool = True,
                connectivity_test: bool = True,
                allowed_subnets: List[str] = None,
                blocked_subnets: List[str] = None,
                event_callback: Optional[Callable] = None):
        
        if dht_port is None:
            dht_port = self._find_available_port(8001)
            logger.info(f"Client using available DHT port: {dht_port}")
        
        # Initialize event-based discovery
        self.dht_discovery = EventBasedDHTDiscovery(
            bootstrap_nodes=bootstrap_nodes,
            dht_port=dht_port,
            enable_subnet_filtering=enable_subnet_filtering,
            connectivity_test=connectivity_test,
            allowed_subnets=allowed_subnets,
            blocked_subnets=blocked_subnets
        )
        
        self.node_selector = NodeSelector(self.dht_discovery)
        self.model = model or "llamanet"
        self.min_tps = min_tps
        self.max_load = max_load
        
        # Event handling
        self.event_listener = ClientEventListener(event_callback)
        self.dht_discovery.add_event_listener(self.event_listener)
        
        # Real-time state
        self.is_started = False
        
        logger.info("Event-aware OpenAI Client initialized")
    
    def _find_available_port(self, start_port: int = 8001) -> int:
        """Find an available port starting from start_port"""
        import socket
        port = start_port
        while port < start_port + 100:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('', port))
                    return port
            except OSError:
                port += 1
        raise RuntimeError(f"No available ports found starting from {start_port}")
    
    async def start(self):
        """Start the event-aware client"""
        if self.is_started:
            return
        
        await self.dht_discovery.start()
        self.is_started = True
        logger.info("Event-aware client started")
    
    async def stop(self):
        """Stop the event-aware client"""
        if not self.is_started:
            return
        
        await self.dht_discovery.stop()
        self.is_started = False
        logger.info("Event-aware client stopped")
    
    async def get_available_nodes(self, model: Optional[str] = None) -> List[NodeInfo]:
        """Get available nodes (real-time, no polling)"""
        if not self.is_started:
            await self.start()
        
        return await self.dht_discovery.get_nodes(model=model)
    
    async def wait_for_nodes(self, min_nodes: int = 1, timeout: float = 30.0) -> bool:
        """Wait for a minimum number of nodes to be available"""
        if not self.is_started:
            await self.start()
        
        start_time = asyncio.get_event_loop().time()
        
        while True:
            nodes = await self.get_available_nodes()
            if len(nodes) >= min_nodes:
                logger.info(f"✅ Found {len(nodes)} nodes (required: {min_nodes})")
                return True
            
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                logger.warning(f"⏰ Timeout waiting for nodes: found {len(nodes)}, required {min_nodes}")
                return False
            
            await asyncio.sleep(1)
    
    def get_real_time_stats(self) -> Dict[str, Any]:
        """Get real-time statistics about the network"""
        if not self.is_started:
            return {"error": "Client not started"}
        
        nodes = list(self.dht_discovery.active_nodes.values())
        
        if not nodes:
            return {
                "total_nodes": 0,
                "models": {},
                "network_health": "no_nodes"
            }
        
        # Group by model
        models = {}
        for node in nodes:
            if node.model not in models:
                models[node.model] = []
            models[node.model].append(node)
        
        # Calculate stats
        total_load = sum(n.load for n in nodes)
        avg_load = total_load / len(nodes)
        total_tps = sum(n.tps for n in nodes)
        
        return {
            "total_nodes": len(nodes),
            "models": {
                model: {
                    "node_count": len(model_nodes),
                    "avg_load": sum(n.load for n in model_nodes) / len(model_nodes),
                    "total_tps": sum(n.tps for n in model_nodes)
                }
                for model, model_nodes in models.items()
            },
            "network_health": "excellent" if avg_load < 0.5 else "good" if avg_load < 0.8 else "poor",
            "avg_load": avg_load,
            "total_capacity": total_tps
        }
    
    async def chat_completions(self, 
                              messages: List[Dict[str, str]],
                              max_tokens: int = 100,
                              temperature: float = 0.7,
                              top_p: float = 0.9,
                              stream: bool = False,
                              stop: Optional[List[str]] = None,
                              strategy: str = "round_robin",
                              max_retries: int = 3) -> Optional[OpenAIChatCompletionResponse]:
        """Create chat completion using OpenAI format"""
        if not self.is_started:
            await self.start()
        
        retries = 0
        
        while retries < max_retries:
            # Select a node with strategy
            node = await self.node_selector.select_node(
                model=self.model,
                min_tps=self.min_tps,
                max_load=self.max_load,
                strategy=strategy
            )
            
            if not node:
                logger.error("No suitable nodes available")
                return None
                
            # Create OpenAI request
            openai_messages = [OpenAIMessage(**msg) for msg in messages]
            request = OpenAIChatCompletionRequest(
                model=self.model,
                messages=openai_messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stream=stream,
                stop=stop
            )
            
            # Send request to node
            try:
                url = f"http://{node.ip}:{node.port}/v1/chat/completions"
                
                if stream:
                    return await self._handle_streaming_request(url, request.dict())
                else:
                    response = requests.post(
                        url,
                        json=request.dict(),
                        headers={"Content-Type": "application/json"},
                        timeout=30
                    )
                    
                    if response.status_code == 200:
                        return OpenAIChatCompletionResponse(**response.json())
                        
                    logger.warning(f"Node {node.node_id} returned error: {response.status_code} {response.text}")
            except Exception as e:
                logger.warning(f"Error with node {node.node_id}: {e}")
                
            retries += 1
            
        logger.error(f"Failed to create chat completion after {max_retries} retries")
        return None
    
    async def completions(self, 
                         prompt: str,
                         max_tokens: int = 100,
                         temperature: float = 0.7,
                         top_p: float = 0.9,
                         stream: bool = False,
                         stop: Optional[List[str]] = None,
                         strategy: str = "round_robin",
                         max_retries: int = 3) -> Optional[OpenAICompletionResponse]:
        """Create completion using OpenAI format"""
        if not self.is_started:
            await self.start()
        
        retries = 0
        
        while retries < max_retries:
            # Select a node with strategy
            node = await self.node_selector.select_node(
                model=self.model,
                min_tps=self.min_tps,
                max_load=self.max_load,
                strategy=strategy
            )
            
            if not node:
                logger.error("No suitable nodes available")
                return None
                
            # Create OpenAI request
            request = OpenAICompletionRequest(
                model=self.model,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stream=stream,
                stop=stop
            )
            
            # Send request to node
            try:
                url = f"http://{node.ip}:{node.port}/v1/completions"
                
                if stream:
                    return await self._handle_streaming_request(url, request.dict())
                else:
                    response = requests.post(
                        url,
                        json=request.dict(),
                        headers={"Content-Type": "application/json"},
                        timeout=30
                    )
                    
                    if response.status_code == 200:
                        return OpenAICompletionResponse(**response.json())
                        
                    logger.warning(f"Node {node.node_id} returned error: {response.status_code} {response.text}")
            except Exception as e:
                logger.warning(f"Error with node {node.node_id}: {e}")
                
            retries += 1
            
        logger.error(f"Failed to create completion after {max_retries} retries")
        return None
    
    async def _handle_streaming_request(self, url: str, request_data: dict):
        """Handle streaming requests"""
        # This would return a streaming response object
        # Implementation depends on your streaming requirements
        logger.info("Streaming request initiated")
        return None
    
    async def close(self):
        """Close the client"""
        await self.stop()

# Backward compatibility alias
EventAwareClient = EventAwareOpenAIClient
