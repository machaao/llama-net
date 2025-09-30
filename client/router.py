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
                         randomize: bool = True,
                         target_model: Optional[str] = None,
                         capability: Optional[str] = None,
                         model_type: Optional[str] = None) -> Optional[NodeInfo]:
        """Select the best node based on criteria, strategy, capabilities, and model type"""
        
        # Use target_model if specified, otherwise fall back to model parameter
        model_filter = target_model or model
        
        # Get nodes from event-based discovery (real-time, no polling)
        nodes = await self.dht_discovery.get_nodes(model=model_filter)
        
        # Filter by capability if specified
        if capability:
            nodes = [node for node in nodes if self._has_capability(node, capability)]
            if not nodes:
                logger.warning(f"No nodes found with capability: {capability}")
                return None
        
        # Filter by model type if specified
        if model_type:
            nodes = [node for node in nodes if node.model_type == model_type]
            if not nodes:
                logger.warning(f"No nodes found with model type: {model_type}")
                return None
            logger.debug(f"Filtered to {len(nodes)} nodes with model type: {model_type}")
        
        if not nodes:
            logger.warning(f"No nodes available for model {model_filter}")
            return None
        
        # If target_model is specified, ensure we only get nodes with that exact model
        if target_model:
            nodes = [node for node in nodes if node.model == target_model]
            if not nodes:
                logger.warning(f"No nodes found running the specific model: {target_model}")
                return None
        
        # Log available nodes for debugging
        logger.debug(f"Available nodes for selection (model: {model_filter}):")
        for node in nodes:
            logger.debug(f"  - {node.node_id[:8]}... at {node.ip}:{node.port} (model: {node.model}, load: {node.load})")
            
        # Filter by criteria (be more lenient for DHT contacts with unknown metrics)
        eligible_nodes = []
        for node in nodes:
            # For nodes with unknown metrics (DHT contacts), be more permissive
            if node.model == "unknown" or node.tps == 0.0:
                eligible_nodes.append(node)  # Include DHT contacts regardless of metrics
            elif node.tps >= min_tps and node.load <= max_load:
                eligible_nodes.append(node)
        
        if not eligible_nodes:
            logger.warning(f"No nodes meet criteria (min_tps={min_tps}, max_load={max_load}) for model {model_filter}")
            # Fall back to any available node with the target model
            eligible_nodes = nodes
        
        # Apply selection strategy with logging
        selected = None
        if strategy == "round_robin":
            selected = self._round_robin_select(eligible_nodes)
        elif strategy == "random":
            selected = random.choice(eligible_nodes)
        elif strategy == "load_balanced":
            selected = self._load_balanced_select(eligible_nodes, randomize)
        else:
            logger.warning(f"Unknown strategy {strategy}, using round_robin")
            selected = self._round_robin_select(eligible_nodes)
        
        if selected:
            logger.info(f"🎯 Selected node {selected.node_id[:8]}... at {selected.ip}:{selected.port} (model: {selected.model}) via {strategy} strategy")
        
        return selected
    
    def _round_robin_select(self, nodes: List[NodeInfo]) -> NodeInfo:
        """Select node using true round robin with persistent state"""
        if not nodes:
            return None
            
        # Sort nodes by node_id for consistent ordering across all clients
        sorted_nodes = sorted(nodes, key=lambda n: n.node_id)
        
        # Use modulo arithmetic for true round-robin
        selected_node = sorted_nodes[self.round_robin_index % len(sorted_nodes)]
        
        # Increment counter for next selection
        self.round_robin_index = (self.round_robin_index + 1) % len(sorted_nodes)
        
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
    
    def _has_capability(self, node: NodeInfo, capability: str) -> bool:
        """Check if node has specific capability"""
        if not node.capabilities:
            return False
        
        return node.capabilities.get(capability, False)
