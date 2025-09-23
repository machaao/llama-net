import asyncio
import requests
import json
from typing import Optional, Dict, Any, List
from common.models import (
    OpenAIChatCompletionRequest, OpenAICompletionRequest,
    OpenAIChatCompletionResponse, OpenAICompletionResponse,
    OpenAIMessage, NodeInfo
)
from client.dht_discovery import DHTDiscovery
from client.router import NodeSelector
from common.utils import get_logger

logger = get_logger(__name__)

class OpenAIClient:
    """OpenAI-compatible client for LlamaNet inference"""
    
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
    
    def __init__(self, 
                bootstrap_nodes: str = "",
                model: Optional[str] = None,
                min_tps: float = 0.0,
                max_load: float = 1.0,
                dht_port: int = None):
        if dht_port is None:
            dht_port = self._find_available_port(8001)
            logger.info(f"Client using available DHT port: {dht_port}")
            
        self.dht_discovery = DHTDiscovery(bootstrap_nodes, dht_port)
        self.node_selector = NodeSelector(self.dht_discovery)
        self.model = model or "llamanet"
        self.min_tps = min_tps
        self.max_load = max_load
        
    async def chat_completions(self, 
                              messages: List[Dict[str, str]],
                              max_tokens: int = 100,
                              temperature: float = 0.7,
                              top_p: float = 0.9,
                              stream: bool = False,
                              stop: Optional[List[str]] = None,
                              strategy: str = "round_robin",  # Add strategy parameter
                              max_retries: int = 3) -> Optional[OpenAIChatCompletionResponse]:
        """Create chat completion using OpenAI format"""
        retries = 0
        
        while retries < max_retries:
            # Select a node with strategy
            node = await self.node_selector.select_node(
                model=self.model,
                min_tps=self.min_tps,
                max_load=self.max_load,
                strategy=strategy  # Pass strategy
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
                
            # Force refresh of nodes cache
            await self.dht_discovery.get_nodes(force_refresh=True)
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
                         strategy: str = "round_robin",  # Add strategy parameter
                         max_retries: int = 3) -> Optional[OpenAICompletionResponse]:
        """Create completion using OpenAI format"""
        retries = 0
        
        while retries < max_retries:
            # Select a node with strategy
            node = await self.node_selector.select_node(
                model=self.model,
                min_tps=self.min_tps,
                max_load=self.max_load,
                strategy=strategy  # Pass strategy
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
                
            # Force refresh of nodes cache
            await self.dht_discovery.get_nodes(force_refresh=True)
            retries += 1
            
        logger.error(f"Failed to create completion after {max_retries} retries")
        return None
    
    async def _handle_streaming_request(self, url: str, request_data: dict):
        """Handle streaming requests"""
        # This would return a streaming response object
        # Implementation depends on your streaming requirements
        logger.info("Streaming request initiated")
        return None
    
    async def get_available_models(self, force_refresh: bool = False) -> Dict[str, List[NodeInfo]]:
        """Get all available models from the network grouped by model name"""
        try:
            # Get all nodes from the network
            all_nodes = await self.dht_discovery.get_nodes(force_refresh=force_refresh)
            
            # Group nodes by model
            models_dict = {}
            for node in all_nodes:
                model_name = node.model
                if model_name not in models_dict:
                    models_dict[model_name] = []
                models_dict[model_name].append(node)
            
            # Sort nodes within each model by load (ascending)
            for model_name in models_dict:
                models_dict[model_name].sort(key=lambda n: n.load)
            
            logger.info(f"Discovered {len(models_dict)} unique models across {len(all_nodes)} nodes")
            return models_dict
            
        except Exception as e:
            logger.error(f"Error getting available models: {e}")
            return {}

    async def get_model_statistics(self) -> Dict[str, Any]:
        """Get statistics about available models on the network"""
        try:
            models_dict = await self.get_available_models()
            
            stats = {
                "total_models": len(models_dict),
                "total_nodes": sum(len(nodes) for nodes in models_dict.values()),
                "models": {}
            }
            
            for model_name, nodes in models_dict.items():
                model_stats = {
                    "node_count": len(nodes),
                    "avg_load": sum(n.load for n in nodes) / len(nodes) if nodes else 0,
                    "avg_tps": sum(n.tps for n in nodes) / len(nodes) if nodes else 0,
                    "best_node": min(nodes, key=lambda n: n.load) if nodes else None,
                    "nodes": [
                        {
                            "node_id": n.node_id,
                            "ip": n.ip,
                            "port": n.port,
                            "load": n.load,
                            "tps": n.tps,
                            "last_seen": n.last_seen
                        } for n in nodes
                    ]
                }
                stats["models"][model_name] = model_stats
            
            return stats
            
        except Exception as e:
            logger.error(f"Error getting model statistics: {e}")
            return {"total_models": 0, "total_nodes": 0, "models": {}}

    async def close(self):
        """Close the client and cleanup resources"""
        await self.dht_discovery.stop()

# Backward compatibility alias
Client = OpenAIClient
