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
        self.round_robin_index = 0  # Track round robin position
        
    async def select_node(self, 
                         model: Optional[str] = None,
                         min_tps: float = 0.0,
                         max_load: float = 1.0,
                         strategy: str = "round_robin",  # "load_balanced", "round_robin", "random"
                         randomize: bool = True) -> Optional[NodeInfo]:
        """Select the best node based on criteria and strategy"""
        # Get nodes from DHT (now includes routing table contacts)
        nodes = await self.dht_discovery.get_nodes(model)
        
        if not nodes:
            logger.warning(f"No nodes available for model {model}")
            return None
            
        # Filter by criteria (be more lenient for DHT contacts with unknown metrics)
        eligible_nodes = []
        for node in nodes:
            # For nodes with unknown metrics (DHT contacts), be more permissive
            if node.model == "unknown" or node.tps == 0.0:
                eligible_nodes.append(node)  # Include DHT contacts regardless of metrics
            elif node.tps >= min_tps and node.load <= max_load:
                eligible_nodes.append(node)
        
        if not eligible_nodes:
            logger.warning(f"No nodes meet criteria (min_tps={min_tps}, max_load={max_load})")
            # Fall back to any available node
            eligible_nodes = nodes
        
        # Apply selection strategy
        if strategy == "round_robin":
            return self._round_robin_select(eligible_nodes)
        elif strategy == "random":
            return random.choice(eligible_nodes)
        elif strategy == "load_balanced":
            return self._load_balanced_select(eligible_nodes, randomize)
        else:
            logger.warning(f"Unknown strategy {strategy}, using round_robin")
            return self._round_robin_select(eligible_nodes)
    
    def _round_robin_select(self, nodes: List[NodeInfo]) -> NodeInfo:
        """Select node using round robin"""
        if not nodes:
            return None
            
        # Sort nodes by node_id for consistent ordering
        sorted_nodes = sorted(nodes, key=lambda n: n.node_id)
        
        # Select next node in round robin
        selected_node = sorted_nodes[self.round_robin_index % len(sorted_nodes)]
        self.round_robin_index += 1
        
        logger.debug(f"Round robin selected node {selected_node.node_id[:8]}... (index: {self.round_robin_index - 1})")
        return selected_node
    
    def _load_balanced_select(self, nodes: List[NodeInfo], randomize: bool) -> NodeInfo:
        """Select node using load balancing (original logic)"""
        # Sort by load (ascending)
        nodes.sort(key=lambda n: n.load)
        
        # Get the best nodes (those with similar load)
        best_load = nodes[0].load
        best_nodes = [n for n in nodes if n.load <= best_load + 0.1]
        
        # Randomize if requested
        if randomize and len(best_nodes) > 1:
            return random.choice(best_nodes)
        
        # Return the first (lowest load)
        return best_nodes[0]
