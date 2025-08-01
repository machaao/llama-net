import random
from typing import List, Optional, Dict, Any, Callable
from common.models import NodeInfo
from client.dht_discovery import DHTDiscovery
from common.utils import get_logger

logger = get_logger(__name__)

class NodeSelector:
    """Select the best node for inference"""
    
    def __init__(self, dht_discovery: DHTDiscovery):
        self.dht_discovery = dht_discovery
        
    async def select_node(self, 
                         model: Optional[str] = None,
                         min_tps: float = 0.0,
                         max_load: float = 1.0,
                         randomize: bool = True) -> Optional[NodeInfo]:
        """Select the best node based on criteria"""
        # Get nodes from DHT
        nodes = await self.dht_discovery.get_nodes(model)
        
        if not nodes:
            logger.warning(f"No nodes available for model {model}")
            return None
            
        # Filter by criteria
        eligible_nodes = [
            node for node in nodes
            if node.tps >= min_tps and node.load <= max_load
        ]
        
        if not eligible_nodes:
            logger.warning(f"No nodes meet criteria (min_tps={min_tps}, max_load={max_load})")
            return None
            
        # Sort by load (ascending)
        eligible_nodes.sort(key=lambda n: n.load)
        
        # Get the best nodes (those with similar load)
        best_load = eligible_nodes[0].load
        best_nodes = [n for n in eligible_nodes if n.load <= best_load + 0.1]
        
        # Randomize if requested
        if randomize and len(best_nodes) > 1:
            return random.choice(best_nodes)
        
        # Return the first (lowest load)
        return best_nodes[0]
