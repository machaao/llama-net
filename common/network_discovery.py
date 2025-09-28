import socket
import ipaddress
import psutil
import asyncio
import aiohttp
from typing import List, Dict, Tuple, Optional, Set
from dataclasses import dataclass
from common.utils import get_logger

logger = get_logger(__name__)

@dataclass
class NetworkInterface:
    """Information about a network interface"""
    name: str
    ip: str
    subnet: str
    network: ipaddress.IPv4Network
    is_up: bool
    is_loopback: bool
    mac_address: Optional[str] = None

@dataclass
class IPClassification:
    """Classification of an IP address"""
    ip: str
    type: str  # 'public', 'private', 'loopback', 'vpn'
    subnet: str
    network: ipaddress.IPv4Network
    interface_name: str
    is_reachable: bool = False
    response_time_ms: Optional[float] = None
    confidence_score: float = 0.0

class NetworkDiscovery:
    """Enhanced network discovery for multi-IP environments"""
    
    def __init__(self, cache_ttl: int = 60):
        self.cache_ttl = cache_ttl
        self._cache = {}
        self._last_discovery = 0
        self._subnet_matcher = None  # Lazy load to avoid circular import
        
    async def discover_all_ips(self, force_refresh: bool = False) -> List[IPClassification]:
        """Discover and classify all available IP addresses"""
        import time
        
        current_time = time.time()
        if not force_refresh and current_time - self._last_discovery < self.cache_ttl:
            return self._cache.get('all_ips', [])
        
        logger.info("🔍 Discovering network interfaces and IP addresses...")
        
        # Get all network interfaces
        interfaces = self._get_network_interfaces()
        
        # Classify each IP
        classifications = []
        for interface in interfaces:
            if interface.is_up and interface.ip != '127.0.0.1':
                classification = self._classify_ip(interface)
                classifications.append(classification)
        
        # Test connectivity for non-loopback IPs
        await self._test_connectivity(classifications)
        
        # Calculate confidence scores
        self._calculate_confidence_scores(classifications)
        
        # Sort by confidence score (highest first)
        classifications.sort(key=lambda x: x.confidence_score, reverse=True)
        
        # Cache results
        self._cache['all_ips'] = classifications
        self._last_discovery = current_time
        
        logger.info(f"✅ Discovered {len(classifications)} IP addresses")
        for ip_class in classifications:
            logger.info(f"   {ip_class.ip} ({ip_class.type}) - {ip_class.interface_name} - Score: {ip_class.confidence_score:.2f}")
        
        return classifications
    
    def _get_network_interfaces(self) -> List[NetworkInterface]:
        """Get all network interfaces using psutil"""
        interfaces = []
        
        try:
            # Get network interface addresses
            net_if_addrs = psutil.net_if_addrs()
            net_if_stats = psutil.net_if_stats()
            
            for interface_name, addresses in net_if_addrs.items():
                # Get interface stats
                stats = net_if_stats.get(interface_name)
                is_up = stats.isup if stats else False
                
                # Process IPv4 addresses
                for addr in addresses:
                    if addr.family == socket.AF_INET:  # IPv4
                        try:
                            ip = addr.address
                            netmask = addr.netmask
                            
                            # Calculate network
                            network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
                            
                            # Get MAC address if available
                            mac_address = None
                            for mac_addr in addresses:
                                if mac_addr.family == psutil.AF_LINK:
                                    mac_address = mac_addr.address
                                    break
                            
                            interface = NetworkInterface(
                                name=interface_name,
                                ip=ip,
                                subnet=str(network),
                                network=network,
                                is_up=is_up,
                                is_loopback=interface_name.startswith('lo') or ip.startswith('127.'),
                                mac_address=mac_address
                            )
                            interfaces.append(interface)
                            
                        except Exception as e:
                            logger.warning(f"Error processing interface {interface_name}: {e}")
                            
        except Exception as e:
            logger.error(f"Error discovering network interfaces: {e}")
        
        return interfaces
    
    def _classify_ip(self, interface: NetworkInterface) -> IPClassification:
        """Classify an IP address by type"""
        ip_obj = ipaddress.IPv4Address(interface.ip)
        
        # Determine IP type
        if ip_obj.is_loopback:
            ip_type = 'loopback'
        elif ip_obj.is_private:
            # Further classify private IPs
            if interface.ip.startswith('192.168.'):
                # Could be VPN or local network
                ip_type = 'vpn' if self._is_likely_vpn_interface(interface) else 'private'
            elif interface.ip.startswith('10.'):
                ip_type = 'vpn' if self._is_likely_vpn_interface(interface) else 'private'
            elif interface.ip.startswith('172.'):
                # 172.16.0.0/12 range
                ip_type = 'vpn' if self._is_likely_vpn_interface(interface) else 'private'
            else:
                ip_type = 'private'
        else:
            ip_type = 'public'
        
        return IPClassification(
            ip=interface.ip,
            type=ip_type,
            subnet=interface.subnet,
            network=interface.network,
            interface_name=interface.name
        )
    
    def _is_likely_vpn_interface(self, interface: NetworkInterface) -> bool:
        """Heuristics to detect if an interface is likely a VPN"""
        vpn_indicators = [
            'tun', 'tap', 'vpn', 'ppp', 'wg', 'utun',
            'nordlynx', 'proton', 'express', 'surfshark'
        ]
        
        interface_name_lower = interface.name.lower()
        
        # Check interface name for VPN indicators
        for indicator in vpn_indicators:
            if indicator in interface_name_lower:
                return True
        
        # Check for common VPN subnet ranges
        if interface.ip.startswith('192.168.'):
            # Many VPNs use 192.168.x.x ranges
            # Additional heuristics could be added here
            pass
        
        return False
    
    async def _test_connectivity(self, classifications: List[IPClassification]):
        """Test external connectivity for each IP"""
        test_urls = [
            'http://httpbin.org/ip',
            'http://icanhazip.com',
            'http://api.ipify.org'
        ]
        
        for classification in classifications:
            if classification.type == 'loopback':
                continue
                
            best_time = float('inf')
            reachable = False
            
            for test_url in test_urls:
                try:
                    # Create connector bound to specific IP
                    connector = aiohttp.TCPConnector(
                        local_addr=(classification.ip, 0),
                        ttl_dns_cache=0,
                        use_dns_cache=False
                    )
                    
                    timeout = aiohttp.ClientTimeout(total=5, connect=2)
                    
                    async with aiohttp.ClientSession(
                        connector=connector, 
                        timeout=timeout
                    ) as session:
                        start_time = asyncio.get_event_loop().time()
                        async with session.get(test_url) as response:
                            if response.status == 200:
                                end_time = asyncio.get_event_loop().time()
                                response_time = (end_time - start_time) * 1000  # Convert to ms
                                
                                if response_time < best_time:
                                    best_time = response_time
                                    reachable = True
                                
                                break  # Success, no need to test other URLs
                                
                except Exception as e:
                    logger.debug(f"Connectivity test failed for {classification.ip} via {test_url}: {e}")
                    continue
            
            classification.is_reachable = reachable
            classification.response_time_ms = best_time if reachable else None
            
            if reachable:
                logger.debug(f"✅ {classification.ip} is reachable ({best_time:.1f}ms)")
            else:
                logger.debug(f"❌ {classification.ip} is not reachable")
    
    def _calculate_confidence_scores(self, classifications: List[IPClassification]):
        """Calculate confidence scores for IP selection"""
        for classification in classifications:
            score = 0.0
            
            # Base score by type
            type_scores = {
                'public': 100.0,
                'private': 80.0,
                'vpn': 60.0,
                'loopback': 10.0
            }
            score += type_scores.get(classification.type, 50.0)
            
            # Reachability bonus
            if classification.is_reachable:
                score += 50.0
                
                # Response time bonus (faster = better)
                if classification.response_time_ms:
                    if classification.response_time_ms < 100:
                        score += 20.0
                    elif classification.response_time_ms < 500:
                        score += 10.0
                    elif classification.response_time_ms < 1000:
                        score += 5.0
            
            # Interface name penalties for known problematic interfaces
            if any(x in classification.interface_name.lower() for x in ['docker', 'veth', 'br-']):
                score -= 30.0
            
            classification.confidence_score = max(0.0, score)
    
    def _get_subnet_matcher(self):
        """Lazy load subnet matcher to avoid circular import"""
        if self._subnet_matcher is None:
            from common.subnet_matcher import get_subnet_matcher
            self._subnet_matcher = get_subnet_matcher()
        return self._subnet_matcher

    async def get_best_ip_for_context(self, context_ips: List[str] = None) -> Optional[str]:
        """Get the best IP address for a given context (e.g., bootstrap nodes)"""
        all_ips = await self.discover_all_ips()
        
        if not all_ips:
            return None
        
        # Use subnet matcher for intelligent IP selection
        subnet_matcher = self._get_subnet_matcher()
        best_ip = subnet_matcher.get_best_ip_for_context(all_ips, context_ips)
        
        if best_ip:
            return best_ip
        
        # Fallback to highest confidence IP
        return all_ips[0].ip if all_ips else None
    
    def _calculate_subnet_compatibility(self, ip_class: IPClassification, context_ips: List[str]) -> float:
        """Calculate how compatible an IP is with context IPs (subnet matching)"""
        compatibility_score = 0.0
        
        for context_ip in context_ips:
            try:
                context_ip_obj = ipaddress.IPv4Address(context_ip)
                
                # Same subnet = high score
                if context_ip_obj in ip_class.network:
                    compatibility_score += 100.0
                    continue
                
                # Same class (A/B/C) = medium score
                if ip_class.ip.split('.')[0] == context_ip.split('.')[0]:
                    compatibility_score += 50.0
                
                # Same type (private/public) = low score
                context_is_private = context_ip_obj.is_private
                local_is_private = ipaddress.IPv4Address(ip_class.ip).is_private
                
                if context_is_private == local_is_private:
                    compatibility_score += 25.0
                    
            except Exception as e:
                logger.debug(f"Error calculating subnet compatibility: {e}")
        
        return compatibility_score / len(context_ips) if context_ips else 0.0
    
    def get_ip_summary(self) -> Dict:
        """Get a summary of discovered IPs for debugging"""
        all_ips = self._cache.get('all_ips', [])
        
        summary = {
            'total_ips': len(all_ips),
            'by_type': {},
            'reachable_count': 0,
            'best_ip': None,
            'ips': []
        }
        
        for ip_class in all_ips:
            # Count by type
            summary['by_type'][ip_class.type] = summary['by_type'].get(ip_class.type, 0) + 1
            
            # Count reachable
            if ip_class.is_reachable:
                summary['reachable_count'] += 1
            
            # Track best IP
            if not summary['best_ip'] and ip_class.is_reachable:
                summary['best_ip'] = ip_class.ip
            
            # Add to list
            summary['ips'].append({
                'ip': ip_class.ip,
                'type': ip_class.type,
                'subnet': ip_class.subnet,
                'interface': ip_class.interface_name,
                'reachable': ip_class.is_reachable,
                'response_time_ms': ip_class.response_time_ms,
                'confidence_score': ip_class.confidence_score
            })
        
        return summary

# Global instance
_network_discovery = None

def get_network_discovery() -> NetworkDiscovery:
    """Get the global network discovery instance"""
    global _network_discovery
    if _network_discovery is None:
        _network_discovery = NetworkDiscovery()
    return _network_discovery
