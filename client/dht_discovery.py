import asyncio
import time
from typing import List, Optional, Dict, Any, Tuple
from dht.kademlia_node import KademliaNode
from common.models import NodeInfo
from common.utils import get_logger
from common.network_utils import NetworkUtils, SubnetFilter
from client.discovery import DiscoveryInterface

logger = get_logger(__name__)

class DHTDiscovery(DiscoveryInterface):
    """Client for discovering nodes via Kademlia DHT"""
    
    def __init__(self, bootstrap_nodes: str = "", dht_port: int = 8001,
                 enable_subnet_filtering: bool = True,
                 connectivity_test: bool = True,
                 allowed_subnets: List[str] = None,
                 blocked_subnets: List[str] = None):
        self.bootstrap_nodes = self._parse_bootstrap_nodes(bootstrap_nodes)
        self.dht_port = dht_port
        self.kademlia_node = None
        self.cache_time = 0
        self.cache_ttl = 5  # Cache results for 5 seconds
        self.nodes_cache: List[NodeInfo] = []
        self._cache_is_model_specific = False
        self.known_node_ids = set()  # Track known nodes
        
        # Network filtering configuration
        self.enable_subnet_filtering = enable_subnet_filtering
        self.connectivity_test = connectivity_test
        self.subnet_filter = SubnetFilter(
            allowed_subnets=allowed_subnets,
            blocked_subnets=blocked_subnets,
            auto_detect_local=enable_subnet_filtering
        ) if enable_subnet_filtering else None
        
        if enable_subnet_filtering:
            logger.info("DHT Discovery initialized with subnet filtering enabled")
        else:
            logger.info("DHT Discovery initialized with subnet filtering disabled")
        
    def _parse_bootstrap_nodes(self, bootstrap_str: str) -> List[Tuple[str, int]]:
        """Parse bootstrap nodes from comma-separated string"""
        if not bootstrap_str:
            return []
        
        nodes = []
        for node_str in bootstrap_str.split(','):
            try:
                ip, port = node_str.strip().split(':')
                nodes.append((ip, int(port)))
            except ValueError:
                logger.warning(f"Invalid bootstrap node format: {node_str}")
        
        return nodes
    
    async def start(self):
        """Start the DHT client using shared DHT service"""
        if self.kademlia_node:
            return
        
        from common.dht_service import SharedDHTService
        dht_service = SharedDHTService()
        
        if dht_service.is_initialized():
            # Use existing shared instance
            self.kademlia_node = dht_service.kademlia_node
            logger.info("DHT discovery using existing shared service")
        else:
            # This shouldn't happen if server starts first, but handle gracefully
            logger.warning("Shared DHT service not initialized, discovery may not work properly")
            return
    
    async def stop(self):
        """Stop the DHT client"""
        # Don't stop the shared DHT service - just clear our reference
        self.kademlia_node = None
    
    async def get_nodes(self, model: Optional[str] = None, force_refresh: bool = False) -> List[NodeInfo]:
        """Get all active nodes, optionally filtered by model"""
        if not self.kademlia_node:
            await self.start()
        
        current_time = time.time()
        
        # Check if we need to refresh the cache
        if force_refresh or current_time - self.cache_time > self.cache_ttl:
            await self._refresh_nodes(model)
        
        # Filter by model if needed and not already filtered
        if model and not self._cache_is_model_specific:
            return [node for node in self.nodes_cache if node.model == model]
        
        return self.nodes_cache
    
    async def _get_routing_table_contacts(self) -> List[NodeInfo]:
        """Get contacts from DHT routing table and convert to NodeInfo"""
        if not self.kademlia_node:
            return []
        
        contacts = self.kademlia_node.routing_table.get_all_contacts()
        node_infos = []
        
        for contact in contacts:
            try:
                # Create basic NodeInfo from contact
                # Try to get actual node info via HTTP first
                http_port = await self._probe_http_port(contact.ip, contact.node_id)
                if http_port is None:
                    http_port = 8000  # Default fallback
                
                # Create NodeInfo with IP:port format
                node_info = NodeInfo(
                    node_id=f"{contact.ip}:{http_port}",
                    ip=contact.ip,
                    port=http_port,
                    model="unknown",
                    last_seen=int(contact.last_seen)
                )
                
                # Get model info
                model_info = await self._get_node_model(contact.ip, http_port)
                if model_info:
                    node_info.model = model_info
                
                node_infos.append(node_info)
                
            except Exception as e:
                logger.debug(f"Failed to get info for contact {contact.node_id[:8]}: {e}")
        
        return node_infos

    async def _probe_http_port(self, ip: str, node_id: str) -> Optional[int]:
        """Probe for the correct HTTP port for a node"""
        import aiohttp
        
        # Try common HTTP ports
        test_ports = [8000, 8002, 8004, 8006, 8008, 8010]
        
        for port in test_ports:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as session:
                    async with session.get(f"http://{ip}:{port}/info") as resp:
                        if resp.status == 200:
                            info = await resp.json()
                            # Verify this is the correct node by checking IP:port format
                            expected_node_id = f"{ip}:{port}"
                            if info.get('node_id') == expected_node_id:
                                logger.debug(f"Found HTTP port {port} for node {expected_node_id}")
                                return port
            except:
                continue
        
        logger.warning(f"Could not find HTTP port for node {node_id[:8]}... on {ip}")
        return None

    async def _get_node_model(self, ip: str, port: int) -> Optional[str]:
        """Get model information from a node"""
        import aiohttp
        
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as session:
                async with session.get(f"http://{ip}:{port}/info") as resp:
                    if resp.status == 200:
                        info = await resp.json()
                        return info.get('model', 'unknown')
        except:
            pass
        
        return 'unknown'

    async def _get_published_nodes(self, model: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get published nodes from DHT storage"""
        nodes = []
        
        if model:
            key = f"model:{model}"
            node_data = await self.kademlia_node.find_value(key)
            if node_data:
                if isinstance(node_data, list):
                    nodes.extend(node_data)
                else:
                    nodes.append(node_data)
            self._cache_is_model_specific = True
        else:
            key = "all_nodes"
            node_data = await self.kademlia_node.find_value(key)
            if node_data:
                if isinstance(node_data, list):
                    nodes.extend(node_data)
                else:
                    nodes.append(node_data)
            self._cache_is_model_specific = False
        
        return nodes

    async def _refresh_nodes(self, model: Optional[str] = None):
        """Refresh the nodes cache from DHT with enhanced discovery and subnet filtering"""
        try:
            # Use a dict to ensure uniqueness by node_id (not ip:port)
            unique_nodes = {}
            
            # 1. First try to get published nodes from DHT storage
            published_nodes = await self._get_published_nodes(model)
            
            # Also try to get from model-specific keys if no model filter
            if not model:
                try:
                    # Try common model names
                    for common_model in ["llamanet", "llama", "mistral"]:
                        model_nodes = await self._get_published_nodes(common_model)
                        published_nodes.extend(model_nodes)
                except Exception as e:
                    logger.debug(f"Error getting model-specific nodes: {e}")
            
            # Process published nodes
            raw_nodes = []
            for node_data in published_nodes:
                if isinstance(node_data, dict) and node_data.get('node_id'):
                    node_id = node_data['node_id']
                    # Use longer timeout for published data (2 minutes)
                    if time.time() - node_data.get('last_seen', 0) < 120:
                        raw_nodes.append(node_data)
                        unique_nodes[node_id] = {
                            'node': node_data,
                            'source': 'published',
                            'priority': 1
                        }
            
            logger.debug(f"Found {len(raw_nodes)} published nodes before filtering")
            
            # Apply subnet filtering if enabled
            if self.enable_subnet_filtering and self.subnet_filter and raw_nodes:
                logger.debug(f"Applying subnet filtering to {len(raw_nodes)} nodes")
                
                # First apply subnet rules
                filtered_nodes = self.subnet_filter.filter_nodes(raw_nodes)
                
                # Then validate reachability if enabled
                if self.connectivity_test and filtered_nodes:
                    filtered_nodes = await NetworkUtils.validate_node_reachability(
                        filtered_nodes, 
                        connectivity_test=True,
                        timeout=3.0
                    )
                
                raw_nodes = filtered_nodes
            
            # Convert to NodeInfo objects and update cache
            self.nodes_cache = []
            for node_data in raw_nodes:
                try:
                    node_info = NodeInfo(**node_data)
                    self.nodes_cache.append(node_info)
                    
                    # Check if this is a new node
                    node_id = node_data.get('node_id')
                    if node_id not in self.known_node_ids:
                        logger.info(f"🆕 New reachable node: {node_id[:12]}... ({node_data.get('ip')}:{node_data.get('port')}) - Model: {node_data.get('model')}")
                        self.known_node_ids.add(node_id)
                except Exception as e:
                    logger.warning(f"Failed to create NodeInfo from {node_data}: {e}")
            
            self.cache_time = time.time()
            
            # Enhanced logging
            total_discovered = len(published_nodes)
            total_reachable = len(self.nodes_cache)
            filtered_count = total_discovered - total_reachable
            
            if self.enable_subnet_filtering:
                logger.info(f"Node discovery: {total_discovered} found, {filtered_count} filtered, {total_reachable} reachable")
            else:
                logger.info(f"Node discovery: {total_reachable} nodes (filtering disabled)")
            
            # Log all discovered nodes for debugging
            for node in self.nodes_cache:
                logger.debug(f"Available node: {node.node_id[:8]}... at {node.ip}:{node.port} (model: {node.model})")
            
            if total_discovered > 0 and total_reachable == 0:
                logger.warning("All discovered nodes were filtered out - check network connectivity and subnet configuration")
            
        except Exception as e:
            logger.error(f"Error refreshing nodes from DHT: {e}")
            self.nodes_cache = []
    
    async def find_specific_node(self, node_id: str) -> Optional[NodeInfo]:
        """Find a specific node by ID"""
        if not self.kademlia_node:
            await self.start()
        
        try:
            key = f"node:{node_id}"
            node_data = await self.kademlia_node.find_value(key)
            if node_data:
                return NodeInfo(**node_data)
        except Exception as e:
            logger.error(f"Error finding node {node_id}: {e}")
        
        return None

    async def get_models_by_category(self, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """Get models organized by categories with detailed statistics"""
        try:
            all_nodes = await self.get_nodes(force_refresh=force_refresh)
            
            models_by_category = {}
            
            for node in all_nodes:
                model_name = node.model
                if model_name not in models_by_category:
                    models_by_category[model_name] = {
                        "model_name": model_name,
                        "nodes": [],
                        "total_nodes": 0,
                        "avg_load": 0.0,
                        "total_tps": 0.0,
                        "best_node": None,
                        "availability_status": "unknown",
                        "last_updated": time.time()
                    }
                
                models_by_category[model_name]["nodes"].append(node)
            
            # Calculate statistics for each model
            for model_name, model_data in models_by_category.items():
                nodes = model_data["nodes"]
                model_data["total_nodes"] = len(nodes)
                
                if nodes:
                    # Calculate averages
                    total_load = sum(n.load for n in nodes)
                    total_tps = sum(n.tps for n in nodes)
                    
                    model_data["avg_load"] = total_load / len(nodes)
                    model_data["total_tps"] = total_tps
                    
                    # Find best node (lowest load)
                    model_data["best_node"] = min(nodes, key=lambda n: n.load)
                    
                    # Determine availability status
                    if len(nodes) >= 3:
                        model_data["availability_status"] = "high"
                    elif len(nodes) >= 2:
                        model_data["availability_status"] = "medium"
                    else:
                        model_data["availability_status"] = "low"
                    
                    # Sort nodes by load for consistent ordering
                    model_data["nodes"].sort(key=lambda n: n.load)
            
            logger.info(f"Categorized {len(models_by_category)} models with detailed statistics")
            return models_by_category
            
        except Exception as e:
            logger.error(f"Error getting models by category: {e}")
            return {}

    async def get_network_health_metrics(self) -> Dict[str, Any]:
        """Get overall network health and performance metrics"""
        try:
            all_nodes = await self.get_nodes(force_refresh=True)
            
            if not all_nodes:
                return {
                    "status": "no_nodes",
                    "total_nodes": 0,
                    "models_available": 0,
                    "network_load": 0.0,
                    "total_capacity": 0.0,
                    "health_score": 0.0
                }
            
            # Calculate network-wide metrics
            total_load = sum(n.load for n in all_nodes)
            total_tps = sum(n.tps for n in all_nodes)
            avg_load = total_load / len(all_nodes)
            
            # Count unique models
            unique_models = len(set(n.model for n in all_nodes))
            
            # Calculate health score (0-100)
            # Based on: node count, load distribution, model diversity
            node_score = min(100, len(all_nodes) * 10)  # 10 points per node, max 100
            load_score = max(0, 100 - (avg_load * 100))  # Lower load = higher score
            diversity_score = min(100, unique_models * 25)  # 25 points per unique model
            
            health_score = (node_score + load_score + diversity_score) / 3
            
            # Determine overall status
            if health_score >= 80:
                status = "excellent"
            elif health_score >= 60:
                status = "good"
            elif health_score >= 40:
                status = "fair"
            else:
                status = "poor"
            
            return {
                "status": status,
                "total_nodes": len(all_nodes),
                "models_available": unique_models,
                "network_load": round(avg_load, 3),
                "total_capacity": round(total_tps, 2),
                "health_score": round(health_score, 1),
                "node_distribution": self._analyze_node_distribution(all_nodes),
                "last_updated": time.time()
            }
            
        except Exception as e:
            logger.error(f"Error getting network health metrics: {e}")
            return {
                "status": "error",
                "error": str(e),
                "total_nodes": 0,
                "models_available": 0
            }

    def _analyze_node_distribution(self, nodes: List[NodeInfo]) -> Dict[str, Any]:
        """Analyze how nodes are distributed across the network"""
        if not nodes:
            return {"by_model": {}, "by_load": {}, "by_performance": {}}
        
        # Distribution by model
        by_model = {}
        for node in nodes:
            model = node.model
            if model not in by_model:
                by_model[model] = 0
            by_model[model] += 1
        
        # Distribution by load ranges
        by_load = {"low": 0, "medium": 0, "high": 0}
        for node in nodes:
            if node.load < 0.3:
                by_load["low"] += 1
            elif node.load < 0.7:
                by_load["medium"] += 1
            else:
                by_load["high"] += 1
        
        # Distribution by performance (TPS)
        by_performance = {"fast": 0, "medium": 0, "slow": 0}
        if nodes:
            avg_tps = sum(n.tps for n in nodes) / len(nodes)
            for node in nodes:
                if node.tps > avg_tps * 1.2:
                    by_performance["fast"] += 1
                elif node.tps > avg_tps * 0.8:
                    by_performance["medium"] += 1
                else:
                    by_performance["slow"] += 1
        
        return {
            "by_model": by_model,
            "by_load": by_load,
            "by_performance": by_performance
        }

    async def discover_model_capabilities(self, model_name: str) -> Dict[str, Any]:
        """Discover detailed capabilities for a specific model"""
        try:
            model_nodes = await self.get_nodes(model=model_name, force_refresh=True)
            
            if not model_nodes:
                return {
                    "model_name": model_name,
                    "available": False,
                    "node_count": 0,
                    "capabilities": {}
                }
            
            # Aggregate capabilities from all nodes running this model
            total_capacity = sum(n.tps for n in model_nodes)
            avg_load = sum(n.load for n in model_nodes) / len(model_nodes)
            
            # Find best and worst performing nodes
            best_node = min(model_nodes, key=lambda n: n.load)
            worst_node = max(model_nodes, key=lambda n: n.load)
            
            # Calculate reliability score based on node availability
            current_time = time.time()
            recent_nodes = [n for n in model_nodes if current_time - n.last_seen < 60]
            reliability_score = len(recent_nodes) / len(model_nodes) * 100
            
            return {
                "model_name": model_name,
                "available": True,
                "node_count": len(model_nodes),
                "capabilities": {
                    "total_capacity_tps": round(total_capacity, 2),
                    "average_load": round(avg_load, 3),
                    "reliability_score": round(reliability_score, 1),
                    "best_node": {
                        "node_id": best_node.node_id,
                        "ip": best_node.ip,
                        "port": best_node.port,
                        "load": best_node.load,
                        "tps": best_node.tps
                    },
                    "performance_range": {
                        "min_load": min(n.load for n in model_nodes),
                        "max_load": max(n.load for n in model_nodes),
                        "min_tps": min(n.tps for n in model_nodes),
                        "max_tps": max(n.tps for n in model_nodes)
                    },
                    "geographic_distribution": self._analyze_geographic_distribution(model_nodes)
                },
                "nodes": [
                    {
                        "node_id": n.node_id,
                        "ip": n.ip,
                        "port": n.port,
                        "load": n.load,
                        "tps": n.tps,
                        "uptime": n.uptime,
                        "last_seen": n.last_seen,
                        "status": "online" if current_time - n.last_seen < 60 else "stale"
                    } for n in model_nodes
                ],
                "last_updated": time.time()
            }
            
        except Exception as e:
            logger.error(f"Error discovering capabilities for model {model_name}: {e}")
            return {
                "model_name": model_name,
                "available": False,
                "error": str(e)
            }

    def _analyze_geographic_distribution(self, nodes: List[NodeInfo]) -> Dict[str, int]:
        """Analyze geographic distribution of nodes (simplified by IP ranges)"""
        distribution = {}
        
        for node in nodes:
            # Simple geographic classification based on IP ranges
            # This is a basic implementation - in production you'd use a proper GeoIP service
            ip_parts = node.ip.split('.')
            if len(ip_parts) >= 2:
                region_key = f"{ip_parts[0]}.{ip_parts[1]}.x.x"
                if region_key not in distribution:
                    distribution[region_key] = 0
                distribution[region_key] += 1
            else:
                if "unknown" not in distribution:
                    distribution["unknown"] = 0
                distribution["unknown"] += 1
        
        return distribution

    def configure_subnet_filtering(self, 
                                 enable: bool = True,
                                 connectivity_test: bool = True,
                                 allowed_subnets: List[str] = None,
                                 blocked_subnets: List[str] = None):
        """Update subnet filtering configuration"""
        self.enable_subnet_filtering = enable
        self.connectivity_test = connectivity_test
        
        if enable:
            self.subnet_filter = SubnetFilter(
                allowed_subnets=allowed_subnets,
                blocked_subnets=blocked_subnets,
                auto_detect_local=True
            )
            logger.info("Subnet filtering configuration updated")
        else:
            self.subnet_filter = None
            logger.info("Subnet filtering disabled")
        
        # Force cache refresh with new settings
        self.cache_time = 0

    async def get_real_time_model_status(self) -> Dict[str, Any]:
        """Get real-time status of all models with live updates"""
        try:
            # Force refresh to get latest data
            all_nodes = await self.get_nodes(force_refresh=True)
            models_data = await self.get_models_by_category(force_refresh=True)
            health_metrics = await self.get_network_health_metrics()
            
            # Combine all data for comprehensive status
            real_time_status = {
                "timestamp": time.time(),
                "network_health": health_metrics,
                "models": {},
                "summary": {
                    "total_models": len(models_data),
                    "total_nodes": len(all_nodes),
                    "active_nodes": len([n for n in all_nodes if time.time() - n.last_seen < 60]),
                    "network_load": health_metrics.get("network_load", 0.0),
                    "total_capacity": health_metrics.get("total_capacity", 0.0)
                }
            }
            
            # Add detailed model information
            for model_name, model_info in models_data.items():
                real_time_status["models"][model_name] = {
                    **model_info,
                    "capabilities": await self.discover_model_capabilities(model_name)
                }
            
            logger.info(f"Generated real-time status for {len(models_data)} models")
            return real_time_status
            
        except Exception as e:
            logger.error(f"Error getting real-time model status: {e}")
            return {
                "timestamp": time.time(),
                "error": str(e),
                "models": {},
                "summary": {"total_models": 0, "total_nodes": 0}
            }
