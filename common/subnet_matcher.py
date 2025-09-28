import ipaddress
from typing import List, Tuple, Dict, Optional
from dataclasses import dataclass
from common.utils import get_logger

logger = get_logger(__name__)

@dataclass
class SubnetMatch:
    """Result of subnet matching analysis"""
    ip: str
    subnet_score: float
    proximity_score: float
    total_score: float
    match_reason: str

class SubnetMatcher:
    """Smart subnet matching for optimal IP selection"""
    
    def __init__(self):
        self.cache = {}
        self.cache_ttl = 300  # 5 minutes
    
    def analyze_bootstrap_context(self, bootstrap_ips: List[str]) -> Dict[str, any]:
        """Analyze bootstrap node IPs to understand network context"""
        if not bootstrap_ips:
            return {"subnets": [], "network_types": [], "analysis": "no_bootstrap_nodes"}
        
        context = {
            "subnets": [],
            "network_types": [],
            "ip_analysis": [],
            "dominant_type": None
        }
        
        for ip_str in bootstrap_ips:
            try:
                ip_obj = ipaddress.IPv4Address(ip_str)
                
                # Determine network type
                if ip_obj.is_private:
                    if ip_str.startswith('192.168.'):
                        net_type = 'private_class_c'
                        subnet = ipaddress.IPv4Network(f"{ip_str}/24", strict=False)
                    elif ip_str.startswith('10.'):
                        net_type = 'private_class_a'
                        subnet = ipaddress.IPv4Network(f"{ip_str}/8", strict=False)
                    elif ip_str.startswith('172.'):
                        net_type = 'private_class_b'
                        subnet = ipaddress.IPv4Network(f"{ip_str}/12", strict=False)
                    else:
                        net_type = 'private_other'
                        subnet = ipaddress.IPv4Network(f"{ip_str}/24", strict=False)
                else:
                    net_type = 'public'
                    subnet = ipaddress.IPv4Network(f"{ip_str}/24", strict=False)
                
                context["subnets"].append(subnet)
                context["network_types"].append(net_type)
                context["ip_analysis"].append({
                    "ip": ip_str,
                    "type": net_type,
                    "subnet": str(subnet),
                    "is_private": ip_obj.is_private
                })
                
            except Exception as e:
                logger.warning(f"Error analyzing bootstrap IP {ip_str}: {e}")
        
        # Determine dominant network type
        if context["network_types"]:
            type_counts = {}
            for net_type in context["network_types"]:
                type_counts[net_type] = type_counts.get(net_type, 0) + 1
            
            context["dominant_type"] = max(type_counts.items(), key=lambda x: x[1])[0]
        
        return context
    
    def rank_ips_for_bootstrap_context(self, 
                                     available_ips: List, 
                                     bootstrap_ips: List[str]) -> List[SubnetMatch]:
        """Rank available IPs based on compatibility with bootstrap context"""
        
        if not bootstrap_ips:
            # No bootstrap context, rank by general preference
            return self._rank_ips_general(available_ips)
        
        # Analyze bootstrap context
        bootstrap_context = self.analyze_bootstrap_context(bootstrap_ips)
        
        matches = []
        
        for ip_class in available_ips:
            if not ip_class.is_reachable and ip_class.type != 'loopback':
                continue
            
            subnet_score = self._calculate_subnet_score(ip_class, bootstrap_context)
            proximity_score = self._calculate_proximity_score(ip_class, bootstrap_ips)
            
            # Combine scores with weights
            total_score = (
                subnet_score * 0.6 +           # Subnet compatibility (60%)
                proximity_score * 0.3 +        # IP proximity (30%)
                ip_class.confidence_score * 0.1 # Base confidence (10%)
            )
            
            match_reason = self._determine_match_reason(ip_class, bootstrap_context, subnet_score, proximity_score)
            
            matches.append(SubnetMatch(
                ip=ip_class.ip,
                subnet_score=subnet_score,
                proximity_score=proximity_score,
                total_score=total_score,
                match_reason=match_reason
            ))
        
        # Sort by total score (highest first)
        matches.sort(key=lambda x: x.total_score, reverse=True)
        
        # Log ranking results
        logger.info(f"🎯 Subnet matching results for bootstrap context {bootstrap_ips}:")
        for i, match in enumerate(matches[:3]):  # Show top 3
            logger.info(f"   {i+1}. {match.ip} (score: {match.total_score:.2f}) - {match.match_reason}")
        
        return matches
    
    def _calculate_subnet_score(self, ip_class, bootstrap_context: Dict) -> float:
        """Calculate subnet compatibility score"""
        score = 0.0
        
        try:
            local_ip = ipaddress.IPv4Address(ip_class.ip)
            
            # Check direct subnet matches
            for bootstrap_subnet in bootstrap_context.get("subnets", []):
                if local_ip in bootstrap_subnet:
                    score += 100.0  # Perfect subnet match
                    break
            else:
                # No direct match, check network type compatibility
                local_is_private = local_ip.is_private
                dominant_type = bootstrap_context.get("dominant_type", "")
                
                if local_is_private and "private" in dominant_type:
                    score += 75.0  # Same private network class
                elif not local_is_private and dominant_type == "public":
                    score += 75.0  # Both public
                elif local_is_private and dominant_type == "public":
                    score += 25.0  # Private to public (lower score)
                elif not local_is_private and "private" in dominant_type:
                    score += 50.0  # Public to private (medium score)
                
                # Additional scoring for IP class proximity
                for analysis in bootstrap_context.get("ip_analysis", []):
                    bootstrap_ip = analysis["ip"]
                    if self._same_ip_class(ip_class.ip, bootstrap_ip):
                        score += 25.0
                        break
        
        except Exception as e:
            logger.debug(f"Error calculating subnet score for {ip_class.ip}: {e}")
        
        return min(score, 100.0)  # Cap at 100
    
    def _calculate_proximity_score(self, ip_class, bootstrap_ips: List[str]) -> float:
        """Calculate IP proximity score"""
        score = 0.0
        
        try:
            local_parts = ip_class.ip.split('.')
            
            for bootstrap_ip in bootstrap_ips:
                bootstrap_parts = bootstrap_ip.split('.')
                
                # Score based on matching octets
                matching_octets = 0
                for i in range(min(len(local_parts), len(bootstrap_parts))):
                    if local_parts[i] == bootstrap_parts[i]:
                        matching_octets += 1
                    else:
                        break
                
                # Convert to score (4 octets = 100 points)
                proximity = (matching_octets / 4.0) * 100.0
                score = max(score, proximity)  # Take best match
        
        except Exception as e:
            logger.debug(f"Error calculating proximity score for {ip_class.ip}: {e}")
        
        return score
    
    def _same_ip_class(self, ip1: str, ip2: str) -> bool:
        """Check if two IPs are in the same class (A/B/C)"""
        try:
            return ip1.split('.')[0] == ip2.split('.')[0]
        except:
            return False
    
    def _determine_match_reason(self, ip_class, bootstrap_context: Dict, 
                               subnet_score: float, proximity_score: float) -> str:
        """Determine the reason for the match score"""
        
        if subnet_score >= 100.0:
            return "perfect_subnet_match"
        elif subnet_score >= 75.0:
            return "same_network_type"
        elif proximity_score >= 75.0:
            return "high_ip_proximity"
        elif subnet_score >= 50.0:
            return "compatible_network_type"
        elif proximity_score >= 50.0:
            return "medium_ip_proximity"
        elif ip_class.is_reachable:
            return "reachable_fallback"
        else:
            return "last_resort"
    
    def _rank_ips_general(self, available_ips: List) -> List[SubnetMatch]:
        """General IP ranking when no bootstrap context is available"""
        matches = []
        
        for ip_class in available_ips:
            # Use confidence score as primary ranking
            total_score = ip_class.confidence_score
            
            match_reason = f"general_ranking_{ip_class.type}"
            if ip_class.is_reachable:
                match_reason += "_reachable"
            
            matches.append(SubnetMatch(
                ip=ip_class.ip,
                subnet_score=0.0,
                proximity_score=0.0,
                total_score=total_score,
                match_reason=match_reason
            ))
        
        matches.sort(key=lambda x: x.total_score, reverse=True)
        return matches
    
    def get_best_ip_for_context(self, available_ips: List, 
                               bootstrap_ips: List[str] = None) -> Optional[str]:
        """Get the single best IP for the given context"""
        
        matches = self.rank_ips_for_bootstrap_context(available_ips, bootstrap_ips or [])
        
        if matches:
            best_match = matches[0]
            logger.info(f"🎯 Selected best IP: {best_match.ip} (score: {best_match.total_score:.2f}, reason: {best_match.match_reason})")
            return best_match.ip
        
        return None

# Global instance
_subnet_matcher = None

def get_subnet_matcher() -> SubnetMatcher:
    """Get the global subnet matcher instance"""
    global _subnet_matcher
    if _subnet_matcher is None:
        _subnet_matcher = SubnetMatcher()
    return _subnet_matcher
