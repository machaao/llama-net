import asyncio
import requests
from typing import Optional, Dict, Any, List
from common.models import GenerationRequest, GenerationResponse
from client.dht_discovery import DHTDiscovery
from client.router import NodeSelector
from common.utils import get_logger

logger = get_logger(__name__)

class Client:
    """LlamaNet client for inference"""
    
    def __init__(self, 
                bootstrap_nodes: str = "",
                model: Optional[str] = None,
                min_tps: float = 0.0,
                max_load: float = 1.0,
                dht_port: int = 8001):
        self.dht_discovery = DHTDiscovery(bootstrap_nodes, dht_port)
        self.node_selector = NodeSelector(self.dht_discovery)
        self.model = model
        self.min_tps = min_tps
        self.max_load = max_load
        
    async def generate(self, 
                      prompt: str,
                      max_tokens: int = 100,
                      temperature: float = 0.7,
                      top_p: float = 0.9,
                      top_k: int = 40,
                      stop: Optional[List[str]] = None,
                      repeat_penalty: float = 1.1,
                      max_retries: int = 3) -> Optional[GenerationResponse]:
        """Generate text using the best available node"""
        retries = 0
        
        while retries < max_retries:
            # Select a node
            node = await self.node_selector.select_node(
                model=self.model,
                min_tps=self.min_tps,
                max_load=self.max_load
            )
            
            if not node:
                logger.error("No suitable nodes available")
                return None
                
            # Create request
            request = GenerationRequest(
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                stop=stop,
                repeat_penalty=repeat_penalty
            )
            
            # Send request to node
            try:
                url = f"http://{node.ip}:{node.port}/generate"
                response = requests.post(
                    url,
                    json=request.dict(),
                    headers={"Content-Type": "application/json"},
                    timeout=30
                )
                
                if response.status_code == 200:
                    return GenerationResponse(**response.json())
                    
                logger.warning(f"Node {node.node_id} returned error: {response.status_code} {response.text}")
            except Exception as e:
                logger.warning(f"Error with node {node.node_id}: {e}")
                
            # Force refresh of nodes cache
            await self.dht_discovery.get_nodes(force_refresh=True)
            retries += 1
            
        logger.error(f"Failed to generate text after {max_retries} retries")
        return None
    
    async def close(self):
        """Close the client and cleanup resources"""
        await self.dht_discovery.stop()
