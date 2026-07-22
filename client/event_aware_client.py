import asyncio
import aiohttp
from typing import List, Optional, Dict, Any, Callable
from client.router import NodeSelector, SimpleNodeDiscovery
from common.models import NodeInfo, OpenAIChatCompletionRequest, OpenAICompletionRequest, OpenAIChatCompletionResponse, OpenAICompletionResponse, OpenAIMessage
from common.utils import get_logger
import requests

logger = get_logger(__name__)


class EventAwareOpenAIClient:
    """OpenAI-compatible client with gateway-based node discovery"""
    
    def __init__(self, 
                gateway_url: str = "",
                bootstrap_nodes: str = "",
                model: Optional[str] = None,
                min_tps: float = 0.0,
                max_load: float = 1.0,
                event_callback: Optional[Callable] = None):
        
        self.gateway_url = gateway_url.rstrip("/") if gateway_url else ""
        
        # Initialize simple in-memory node discovery
        self.node_discovery = SimpleNodeDiscovery()
        
        self.node_selector = NodeSelector(self.node_discovery)
        self.model = model or "llamanet"
        self.min_tps = min_tps
        self.max_load = max_load
        self.event_callback = event_callback
        
        # Real-time state
        self.is_started = False
        self._refresh_task: Optional[asyncio.Task] = None
        
        logger.info("Event-aware OpenAI Client initialized")
    
    async def start(self):
        """Start the client and begin refreshing peers from gateway"""
        if self.is_started:
            return
        
        self.is_started = True
        
        # Initial peer fetch
        if self.gateway_url:
            await self._refresh_peers_from_gateway()
            self._refresh_task = asyncio.create_task(self._peer_refresh_loop())
        
        logger.info("Event-aware client started")
    
    async def stop(self):
        """Stop the client"""
        if not self.is_started:
            return
        
        self.is_started = False
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        
        logger.info("Event-aware client stopped")
    
    async def get_available_nodes(self, model: Optional[str] = None) -> List[NodeInfo]:
        """Get available nodes"""
        if not self.is_started:
            await self.start()
        
        return await self.node_discovery.get_nodes(model=model)
    
    async def _peer_refresh_loop(self):
        """Periodically refresh peer list from gateway"""
        while self.is_started:
            try:
                await self._refresh_peers_from_gateway()
            except Exception as e:
                logger.debug(f"Peer refresh error: {e}")
            await asyncio.sleep(30)
    
    async def _refresh_peers_from_gateway(self):
        """Fetch peers from gateway and update discovery"""
        if not self.gateway_url:
            return
        
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.gateway_url}/api/models") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.node_discovery.clear()
                        for model_info in data.get("models", []):
                            model_name = model_info.get("model_name", "unknown")
                            for node in model_info.get("nodes", []):
                                node_info = NodeInfo(
                                    node_id=node.get("node_hash", ""),
                                    ip=node.get("ip", ""),
                                    port=node.get("port", 8000),
                                    model=model_name,
                                    load=node.get("load", 0),
                                    tps=node.get("tps", 0),
                                    url=node.get("url", ""),
                                    gpu_info=node.get("gpu_info", ""),
                                    total_tokens=node.get("total_tokens", 0),
                                )
                                self.node_discovery.add_node(node_info)
                        logger.debug(f"Refreshed {self.node_discovery.count} peers from gateway")
        except Exception as e:
            logger.debug(f"Gateway peer refresh failed: {e}")
    
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
                url = f"{node.url.rstrip('/')}/v1/chat/completions" if node.url else f"http://{node.ip}:{node.port}/v1/chat/completions"
                
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
                url = f"{node.url.rstrip('/')}/v1/completions" if node.url else f"http://{node.ip}:{node.port}/v1/completions"
                
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

# Backward compatibility aliases
EventAwareClient = EventAwareOpenAIClient
OpenAIClient = EventAwareOpenAIClient
