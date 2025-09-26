import asyncio
import time
import json
from typing import Dict, Any, List
from client.dht_discovery import DHTDiscovery
from client.api import OpenAIClient
from common.network_utils import NetworkUtils
from common.utils import get_logger

logger = get_logger(__name__)

class NetworkDiagnostics:
    """Comprehensive network diagnostics for LlamaNet"""
    
    def __init__(self, bootstrap_nodes: str = "localhost:8001"):
        self.bootstrap_nodes = bootstrap_nodes
        self.client = None
        
    async def run_full_diagnostics(self) -> Dict[str, Any]:
        """Run comprehensive network diagnostics"""
        logger.info("🔍 Starting comprehensive network diagnostics...")
        
        # Initialize client with filtering disabled for full discovery
        self.client = OpenAIClient(
            bootstrap_nodes=self.bootstrap_nodes,
            enable_subnet_filtering=False,
            connectivity_test=False
        )
        
        try:
            diagnostics = {
                "timestamp": time.time(),
                "local_network_info": await self._analyze_local_network(),
                "node_discovery": await self._test_node_discovery(),
                "subnet_filtering": await self._test_subnet_filtering(),
                "connectivity_tests": await self._test_connectivity(),
                "performance_analysis": await self._analyze_performance(),
                "recommendations": []
            }
            
            # Generate recommendations
            diagnostics["recommendations"] = self._generate_recommendations(diagnostics)
            
            return diagnostics
            
        finally:
            if self.client:
                await self.client.close()
    
    async def _analyze_local_network(self) -> Dict[str, Any]:
        """Analyze local network configuration"""
        logger.info("📡 Analyzing local network configuration...")
        
        local_subnets = NetworkUtils.get_local_subnets()
        
        return {
            "local_subnets": [str(subnet) for subnet in local_subnets],
            "subnet_count": len(local_subnets),
            "primary_subnet": str(local_subnets[0]) if local_subnets else None,
            "has_multiple_interfaces": len(local_subnets) > 1
        }
    
    async def _test_node_discovery(self) -> Dict[str, Any]:
        """Test node discovery capabilities"""
        logger.info("🔍 Testing node discovery...")
        
        # Test discovery without filtering
        all_nodes = await self.client.dht_discovery.get_nodes(force_refresh=True)
        
        # Group by model
        models = {}
        for node in all_nodes:
            if node.model not in models:
                models[node.model] = []
            models[node.model].append({
                "node_id": node.node_id[:12] + "...",
                "ip": node.ip,
                "port": node.port,
                "load": node.load,
                "tps": node.tps,
                "last_seen": node.last_seen
            })
        
        return {
            "total_nodes_discovered": len(all_nodes),
            "unique_models": len(models),
            "models": models,
            "discovery_successful": len(all_nodes) > 0
        }
    
    async def _test_subnet_filtering(self) -> Dict[str, Any]:
        """Test subnet filtering effectiveness"""
        logger.info("🔒 Testing subnet filtering...")
        
        # Get all nodes without filtering
        all_nodes = await self.client.dht_discovery.get_nodes()
        
        # Enable filtering and get filtered nodes
        self.client.configure_subnet_filtering(enable=True, connectivity_test=False)
        filtered_nodes = await self.client.dht_discovery.get_nodes(force_refresh=True)
        
        # Analyze filtering results
        filtered_count = len(all_nodes) - len(filtered_nodes)
        
        # Categorize nodes by reachability
        local_subnets = NetworkUtils.get_local_subnets()
        reachable_ips = []
        unreachable_ips = []
        
        for node in all_nodes:
            if NetworkUtils.is_ip_in_reachable_subnet(node.ip, local_subnets):
                reachable_ips.append(f"{node.ip}:{node.port}")
            else:
                unreachable_ips.append(f"{node.ip}:{node.port}")
        
        return {
            "total_nodes": len(all_nodes),
            "filtered_nodes": len(filtered_nodes),
            "filtered_count": filtered_count,
            "filtering_effectiveness": f"{filtered_count}/{len(all_nodes)} filtered",
            "reachable_ips": reachable_ips,
            "unreachable_ips": unreachable_ips,
            "local_subnets": [str(subnet) for subnet in local_subnets]
        }
    
    async def _test_connectivity(self) -> Dict[str, Any]:
        """Test actual connectivity to discovered nodes"""
        logger.info("🌐 Testing connectivity to nodes...")
        
        # Get all nodes
        all_nodes = await self.client.dht_discovery.get_nodes()
        
        connectivity_results = []
        successful_connections = 0
        
        for node in all_nodes:
            logger.debug(f"Testing connectivity to {node.ip}:{node.port}")
            
            # Test HTTP connectivity
            http_success = await NetworkUtils.test_http_connectivity(
                node.ip, node.port, timeout=5.0
            )
            
            # Test TCP connectivity
            tcp_success = await NetworkUtils.test_tcp_connectivity(
                node.ip, node.port, timeout=3.0
            )
            
            result = {
                "node_id": node.node_id[:12] + "...",
                "ip": node.ip,
                "port": node.port,
                "model": node.model,
                "http_connectivity": http_success,
                "tcp_connectivity": tcp_success,
                "overall_success": http_success and tcp_success
            }
            
            connectivity_results.append(result)
            
            if result["overall_success"]:
                successful_connections += 1
        
        return {
            "total_tested": len(connectivity_results),
            "successful_connections": successful_connections,
            "success_rate": f"{successful_connections}/{len(connectivity_results)}" if connectivity_results else "0/0",
            "connectivity_details": connectivity_results
        }
    
    async def _analyze_performance(self) -> Dict[str, Any]:
        """Analyze network performance characteristics"""
        logger.info("⚡ Analyzing network performance...")
        
        nodes = await self.client.dht_discovery.get_nodes()
        
        if not nodes:
            return {"error": "No nodes available for performance analysis"}
        
        # Calculate performance metrics
        loads = [node.load for node in nodes]
        tps_values = [node.tps for node in nodes]
        uptimes = [node.uptime for node in nodes]
        
        avg_load = sum(loads) / len(loads)
        avg_tps = sum(tps_values) / len(tps_values)
        avg_uptime = sum(uptimes) / len(uptimes)
        
        # Find best and worst performing nodes
        best_load_node = min(nodes, key=lambda n: n.load)
        worst_load_node = max(nodes, key=lambda n: n.load)
        best_tps_node = max(nodes, key=lambda n: n.tps)
        
        return {
            "node_count": len(nodes),
            "average_load": round(avg_load, 3),
            "average_tps": round(avg_tps, 2),
            "average_uptime_hours": round(avg_uptime / 3600, 2),
            "load_range": {
                "min": min(loads),
                "max": max(loads),
                "best_node": f"{best_load_node.node_id[:12]}... ({best_load_node.ip}:{best_load_node.port})"
            },
            "tps_range": {
                "min": min(tps_values),
                "max": max(tps_values),
                "best_node": f"{best_tps_node.node_id[:12]}... ({best_tps_node.ip}:{best_tps_node.port})"
            },
            "network_health": self._calculate_network_health(avg_load, avg_tps, len(nodes))
        }
    
    def _calculate_network_health(self, avg_load: float, avg_tps: float, node_count: int) -> str:
        """Calculate overall network health status"""
        # Simple health calculation
        load_score = max(0, 100 - (avg_load * 100))
        tps_score = min(100, avg_tps * 10)
        node_score = min(100, node_count * 20)
        
        overall_score = (load_score + tps_score + node_score) / 3
        
        if overall_score >= 80:
            return "excellent"
        elif overall_score >= 60:
            return "good"
        elif overall_score >= 40:
            return "fair"
        else:
            return "poor"
    
    def _generate_recommendations(self, diagnostics: Dict[str, Any]) -> List[str]:
        """Generate recommendations based on diagnostics"""
        recommendations = []
        
        # Check node discovery
        discovery = diagnostics.get("node_discovery", {})
        if discovery.get("total_nodes_discovered", 0) == 0:
            recommendations.append("❌ No nodes discovered. Check bootstrap nodes configuration.")
        elif discovery.get("total_nodes_discovered", 0) < 2:
            recommendations.append("⚠️ Only one node discovered. Consider adding more nodes for redundancy.")
        
        # Check subnet filtering
        filtering = diagnostics.get("subnet_filtering", {})
        if filtering.get("filtered_count", 0) > 0:
            recommendations.append(f"✅ Subnet filtering is working: {filtering.get('filtering_effectiveness', 'unknown')} nodes filtered.")
        
        # Check connectivity
        connectivity = diagnostics.get("connectivity_tests", {})
        success_rate = connectivity.get("successful_connections", 0) / max(1, connectivity.get("total_tested", 1))
        if success_rate < 0.5:
            recommendations.append("❌ Low connectivity success rate. Check network configuration and firewall settings.")
        elif success_rate < 0.8:
            recommendations.append("⚠️ Moderate connectivity issues detected. Some nodes may be unreachable.")
        else:
            recommendations.append("✅ Good connectivity to discovered nodes.")
        
        # Check performance
        performance = diagnostics.get("performance_analysis", {})
        if performance.get("network_health") == "poor":
            recommendations.append("❌ Poor network performance detected. Consider optimizing node configurations.")
        elif performance.get("network_health") == "fair":
            recommendations.append("⚠️ Network performance could be improved.")
        else:
            recommendations.append("✅ Good network performance.")
        
        # Check local network
        local_net = diagnostics.get("local_network_info", {})
        if local_net.get("subnet_count", 0) == 0:
            recommendations.append("❌ Could not detect local subnets. Network configuration may be problematic.")
        
        return recommendations

async def run_diagnostics(bootstrap_nodes: str = "localhost:8001"):
    """Run network diagnostics and print results"""
    diagnostics_tool = NetworkDiagnostics(bootstrap_nodes)
    
    try:
        results = await diagnostics_tool.run_full_diagnostics()
        
        print("🔍 LlamaNet Network Diagnostics Report")
        print("=" * 50)
        print(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(results['timestamp']))}")
        print()
        
        # Local Network Info
        local_info = results["local_network_info"]
        print("📡 Local Network Configuration:")
        print(f"  Local Subnets: {', '.join(local_info['local_subnets'])}")
        print(f"  Multiple Interfaces: {local_info['has_multiple_interfaces']}")
        print()
        
        # Node Discovery
        discovery = results["node_discovery"]
        print("🔍 Node Discovery:")
        print(f"  Total Nodes: {discovery['total_nodes_discovered']}")
        print(f"  Unique Models: {discovery['unique_models']}")
        print(f"  Discovery Status: {'✅ Success' if discovery['discovery_successful'] else '❌ Failed'}")
        print()
        
        # Subnet Filtering
        filtering = results["subnet_filtering"]
        print("🔒 Subnet Filtering:")
        print(f"  Filtering Effectiveness: {filtering['filtering_effectiveness']}")
        print(f"  Reachable IPs: {len(filtering['reachable_ips'])}")
        print(f"  Unreachable IPs: {len(filtering['unreachable_ips'])}")
        if filtering['unreachable_ips']:
            print(f"  Filtered IPs: {', '.join(filtering['unreachable_ips'][:5])}")  # Show first 5
        print()
        
        # Connectivity
        connectivity = results["connectivity_tests"]
        print("🌐 Connectivity Tests:")
        print(f"  Success Rate: {connectivity['success_rate']}")
        print(f"  Successful Connections: {connectivity['successful_connections']}")
        print()
        
        # Performance
        performance = results["performance_analysis"]
        if "error" not in performance:
            print("⚡ Performance Analysis:")
            print(f"  Network Health: {performance['network_health'].upper()}")
            print(f"  Average Load: {performance['average_load']}")
            print(f"  Average TPS: {performance['average_tps']}")
            print()
        
        # Recommendations
        print("💡 Recommendations:")
        for rec in results["recommendations"]:
            print(f"  {rec}")
        
        return results
        
    except Exception as e:
        logger.error(f"Diagnostics failed: {e}")
        print(f"❌ Diagnostics failed: {e}")
        return None

if __name__ == "__main__":
    import sys
    bootstrap = sys.argv[1] if len(sys.argv) > 1 else "localhost:8001"
    asyncio.run(run_diagnostics(bootstrap))
