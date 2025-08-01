import requests
from typing import Optional, Dict, Any, List
from common.models import GenerationRequest, GenerationResponse
from client.discovery import RegistryClient
from client.router import NodeSelector
from common.utils import get_logger

logger = get_logger(__name__)

class Client:
    """LlamaNet client for inference"""
    
    def __init__(self, 
                registry_url: str = "http://localhost:8080",
                model: Optional[str] = None,
                min_tps: float = 0.0,
                max_load: float = 1.0):
        self.registry_client = RegistryClient(registry_url)
        self.node_selector = NodeSelector(self.registry_client)
        self.model = model
        self.min_tps = min_tps
        self.max_load = max_load
        
    def generate(self, 
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
            node = self.node_selector.select_node(
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
            self.registry_client.get_nodes(force_refresh=True)
            retries += 1
            
        logger.error(f"Failed to generate text after {max_retries} retries")
        return None
