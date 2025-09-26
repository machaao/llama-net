import ipaddress
import socket
import asyncio
import aiohttp
import time
from typing import List, Tuple, Optional, Dict, Any
from common.utils import get_logger

logger = get_logger(__name__)

class NetworkUtils:
    """Network utilities for subnet validation and reachability testing"""
    
    @staticmethod
    def get_local_subnets() -> List[ipaddress.IPv4Network]:
        """Get all local network subnets this machine is connected to"""
        subnets = []
        try:
            import psutil
            for interface, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family == socket.AF_INET and addr.address != '127.0.0.1':
                        try:
                            # Create network from IP and netmask
                            network = ipaddress.IPv4Network(
                                f"{addr.address}/{addr.netmask}", 
                                strict=False
                            )
                            subnets.append(network)
                            logger.debug(f"Detected local subnet: {network} on interface {interface}")
                        except (ipaddress.AddressValueError, ValueError) as e:
                            logger.debug(f"Skipping invalid network {addr.address}/{addr.netmask}: {e}")
        except ImportError:
            logger.warning("psutil not available, using fallback method")
            # Fallback: get primary interface subnet
            try:
                from common.utils import get_host_ip
                local_ip = get_host_ip()
                # Assume /24 subnet for fallback
                network = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
                subnets.append(network)
                logger.info(f"Fallback: detected subnet {network}")
            except Exception as e:
                logger.error(f"Failed to determine local subnets: {e}")
        
        return subnets
    
    @staticmethod
    def is_ip_in_reachable_subnet(ip: str, local_subnets: List[ipaddress.IPv4Network] = None) -> bool:
        """Check if an IP address is in a reachable subnet"""
        try:
            target_ip = ipaddress.IPv4Address(ip)
            
            # Get local subnets if not provided
            if local_subnets is None:
                local_subnets = NetworkUtils.get_local_subnets()
            
            # Check for localhost/loopback first
            if target_ip.is_loopback:
                return True
            
            # Check if IP is in any local subnet
            for subnet in local_subnets:
                if target_ip in subnet:
                    logger.debug(f"IP {ip} is reachable (in subnet {subnet})")
                    return True
            
            logger.debug(f"IP {ip} is not in any local subnet: {[str(s) for s in local_subnets]}")
            return False
            
        except (ipaddress.AddressValueError, ValueError) as e:
            logger.warning(f"Invalid IP address {ip}: {e}")
            return False
    
    @staticmethod
    async def test_tcp_connectivity(ip: str, port: int, timeout: float = 3.0) -> bool:
        """Test TCP connectivity to a specific IP:port"""
        try:
            # Use asyncio to test connection
            future = asyncio.open_connection(ip, port)
            reader, writer = await asyncio.wait_for(future, timeout=timeout)
            writer.close()
            await writer.wait_closed()
            logger.debug(f"TCP connectivity successful to {ip}:{port}")
            return True
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
            logger.debug(f"TCP connectivity failed for {ip}:{port}: {type(e).__name__}")
            return False
    
    @staticmethod
    async def test_http_connectivity(ip: str, port: int, timeout: float = 3.0) -> bool:
        """Test HTTP connectivity to a node's API endpoint"""
        try:
            # Try /health endpoint first, then /info as fallback
            endpoints = ["/health", "/info"]
            
            for endpoint in endpoints:
                url = f"http://{ip}:{port}{endpoint}"
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as session:
                        async with session.get(url) as response:
                            if response.status in [200, 404]:  # 404 is ok, means server is responding
                                logger.debug(f"HTTP connectivity successful to {ip}:{port}{endpoint}")
                                return True
                except Exception:
                    continue
            
            logger.debug(f"HTTP connectivity failed for {ip}:{port}")
            return False
            
        except Exception as e:
            logger.debug(f"HTTP connectivity test error for {ip}:{port}: {e}")
            return False
    
    @staticmethod
    def filter_reachable_ips(ip_port_pairs: List[Tuple[str, int]], 
                           local_subnets: List[ipaddress.IPv4Network] = None) -> List[Tuple[str, int]]:
        """Filter IP:port pairs to only include those in reachable subnets"""
        if local_subnets is None:
            local_subnets = NetworkUtils.get_local_subnets()
        
        reachable = []
        filtered_count = 0
        
        for ip, port in ip_port_pairs:
            if NetworkUtils.is_ip_in_reachable_subnet(ip, local_subnets):
                reachable.append((ip, port))
            else:
                logger.debug(f"Filtering out unreachable IP {ip}:{port} (not in local subnets)")
                filtered_count += 1
        
        if filtered_count > 0:
            logger.info(f"Subnet filtering: kept {len(reachable)}, filtered {filtered_count} unreachable IPs")
        
        return reachable
    
    @staticmethod
    async def validate_node_reachability(nodes: List[Dict[str, Any]], 
                                       connectivity_test: bool = True,
                                       timeout: float = 3.0) -> List[Dict[str, Any]]:
        """Validate that nodes are reachable and filter out unreachable ones"""
        local_subnets = NetworkUtils.get_local_subnets()
        logger.info(f"Local subnets detected: {[str(subnet) for subnet in local_subnets]}")
        
        reachable_nodes = []
        subnet_filtered = 0
        connectivity_filtered = 0
        
        for node in nodes:
            ip = node.get('ip')
            port = node.get('port')
            node_id = node.get('node_id', 'unknown')[:12]
            
            if not ip or not port:
                logger.warning(f"Node {node_id} missing IP or port")
                continue
            
            # First check subnet reachability
            if not NetworkUtils.is_ip_in_reachable_subnet(ip, local_subnets):
                logger.debug(f"Node {node_id} at {ip}:{port} filtered out (unreachable subnet)")
                subnet_filtered += 1
                continue
            
            # Optionally test actual connectivity
            if connectivity_test:
                if await NetworkUtils.test_http_connectivity(ip, port, timeout):
                    reachable_nodes.append(node)
                    logger.debug(f"Node {node_id} at {ip}:{port} validated as reachable")
                else:
                    logger.debug(f"Node {node_id} at {ip}:{port} failed connectivity test")
                    connectivity_filtered += 1
            else:
                reachable_nodes.append(node)
        
        total_filtered = subnet_filtered + connectivity_filtered
        if total_filtered > 0:
            logger.info(f"Reachability filtering: kept {len(reachable_nodes)}, "
                       f"filtered {subnet_filtered} subnet + {connectivity_filtered} connectivity")
        
        return reachable_nodes

class SubnetFilter:
    """Configurable subnet filtering with rules"""
    
    def __init__(self, 
                 allowed_subnets: List[str] = None,
                 blocked_subnets: List[str] = None,
                 auto_detect_local: bool = True):
        self.allowed_subnets = self._parse_subnets(allowed_subnets or [])
        self.blocked_subnets = self._parse_subnets(blocked_subnets or [])
        self.auto_detect_local = auto_detect_local
        
        if auto_detect_local:
            self.local_subnets = NetworkUtils.get_local_subnets()
            logger.info(f"SubnetFilter initialized with local subnets: {[str(s) for s in self.local_subnets]}")
        else:
            self.local_subnets = []
    
    def _parse_subnets(self, subnet_strings: List[str]) -> List[ipaddress.IPv4Network]:
        """Parse subnet strings into IPv4Network objects"""
        subnets = []
        for subnet_str in subnet_strings:
            try:
                subnet = ipaddress.IPv4Network(subnet_str, strict=False)
                subnets.append(subnet)
                logger.info(f"Added subnet rule: {subnet}")
            except (ipaddress.AddressValueError, ValueError) as e:
                logger.warning(f"Invalid subnet {subnet_str}: {e}")
        return subnets
    
    def is_ip_allowed(self, ip: str) -> bool:
        """Check if an IP is allowed based on filtering rules"""
        try:
            target_ip = ipaddress.IPv4Address(ip)
            
            # Check blocked subnets first
            for blocked_subnet in self.blocked_subnets:
                if target_ip in blocked_subnet:
                    logger.debug(f"IP {ip} blocked by rule: {blocked_subnet}")
                    return False
            
            # If we have allowed subnets, IP must be in one of them
            if self.allowed_subnets:
                for allowed_subnet in self.allowed_subnets:
                    if target_ip in allowed_subnet:
                        logger.debug(f"IP {ip} allowed by rule: {allowed_subnet}")
                        return True
                logger.debug(f"IP {ip} not in any allowed subnet")
                return False
            
            # If auto-detecting local subnets, check those
            if self.auto_detect_local:
                return NetworkUtils.is_ip_in_reachable_subnet(ip, self.local_subnets)
            
            # Default: allow all if no specific rules
            return True
            
        except (ipaddress.AddressValueError, ValueError):
            logger.warning(f"Invalid IP address {ip}")
            return False
    
    def filter_nodes(self, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter nodes based on subnet rules"""
        filtered_nodes = []
        filtered_count = 0
        
        for node in nodes:
            ip = node.get('ip')
            if ip and self.is_ip_allowed(ip):
                filtered_nodes.append(node)
            else:
                node_id = node.get('node_id', 'unknown')[:12]
                logger.debug(f"Node {node_id} at {ip} filtered out by subnet rules")
                filtered_count += 1
        
        if filtered_count > 0:
            logger.info(f"Subnet rules filtering: kept {len(filtered_nodes)}, filtered {filtered_count}")
        
        return filtered_nodes
