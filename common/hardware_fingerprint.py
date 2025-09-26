import hashlib
import platform
import uuid
import psutil
import socket
import os
from typing import Dict, List, Optional
from common.utils import get_logger

logger = get_logger(__name__)

class HardwareFingerprint:
    """Generate consistent hardware-based fingerprints for node identification"""
    
    def __init__(self):
        self.fingerprint_data = {}
        self._collect_hardware_info()
    
    def _collect_hardware_info(self) -> None:
        """Collect hardware information for fingerprinting"""
        try:
            # CPU information
            self.fingerprint_data['cpu_count'] = psutil.cpu_count(logical=False)
            self.fingerprint_data['cpu_count_logical'] = psutil.cpu_count(logical=True)
            
            # Memory information (total RAM in GB, rounded)
            memory_gb = round(psutil.virtual_memory().total / (1024**3))
            self.fingerprint_data['memory_gb'] = memory_gb
            
            # Platform information
            self.fingerprint_data['platform'] = platform.platform()
            self.fingerprint_data['machine'] = platform.machine()
            self.fingerprint_data['processor'] = platform.processor()
            
            # Network interfaces (MAC addresses)
            self.fingerprint_data['mac_addresses'] = self._get_mac_addresses()
            
            # System UUID (if available)
            self.fingerprint_data['system_uuid'] = self._get_system_uuid()
            
            # Hostname (as fallback)
            self.fingerprint_data['hostname'] = socket.gethostname()
            
            logger.debug(f"Collected hardware fingerprint data: {self._sanitize_for_log()}")
            
        except Exception as e:
            logger.warning(f"Error collecting hardware info: {e}")
            # Fallback to minimal info
            self.fingerprint_data = {
                'hostname': socket.gethostname(),
                'platform': platform.platform(),
                'fallback': True
            }
    
    def _get_mac_addresses(self) -> List[str]:
        """Get MAC addresses from network interfaces"""
        mac_addresses = []
        try:
            import psutil
            for interface, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family == psutil.AF_LINK:  # MAC address
                        mac = addr.address
                        # Filter out virtual/temporary interfaces
                        if (mac and mac != '00:00:00:00:00:00' and 
                            not interface.startswith(('veth', 'docker', 'br-', 'lo'))):
                            mac_addresses.append(mac.upper())
        except Exception as e:
            logger.debug(f"Error getting MAC addresses: {e}")
        
        # Sort for consistency
        return sorted(list(set(mac_addresses)))
    
    def _get_system_uuid(self) -> Optional[str]:
        """Get system UUID if available"""
        try:
            # Try different methods to get system UUID
            uuid_sources = [
                '/sys/class/dmi/id/product_uuid',
                '/proc/sys/kernel/random/uuid'
            ]
            
            for source in uuid_sources:
                try:
                    with open(source, 'r') as f:
                        system_uuid = f.read().strip()
                        if system_uuid and len(system_uuid) > 10:
                            return system_uuid
                except (FileNotFoundError, PermissionError):
                    continue
            
            # Fallback to Python's uuid if available
            try:
                return str(uuid.uuid1())
            except:
                return None
                
        except Exception as e:
            logger.debug(f"Error getting system UUID: {e}")
            return None
    
    def generate_node_id(self, port: int = None) -> str:
        """Generate a consistent hardware-based node ID"""
        # Create a deterministic string from hardware info
        fingerprint_parts = []
        
        # Primary identifiers (most stable)
        if self.fingerprint_data.get('mac_addresses'):
            fingerprint_parts.extend(self.fingerprint_data['mac_addresses'])
        
        if self.fingerprint_data.get('system_uuid'):
            fingerprint_parts.append(self.fingerprint_data['system_uuid'])
        
        # Secondary identifiers
        fingerprint_parts.extend([
            str(self.fingerprint_data.get('cpu_count', 0)),
            str(self.fingerprint_data.get('memory_gb', 0)),
            self.fingerprint_data.get('machine', ''),
            self.fingerprint_data.get('hostname', '')
        ])
        
        # Include port for uniqueness when multiple nodes on same hardware
        if port:
            fingerprint_parts.append(f"port:{port}")
        
        # Create deterministic hash
        fingerprint_string = '|'.join(filter(None, fingerprint_parts))
        
        if not fingerprint_string:
            logger.warning("No hardware fingerprint data available, using fallback")
            fingerprint_string = f"fallback:{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        
        # Generate SHA-1 hash for Kademlia compatibility (160-bit)
        node_id = hashlib.sha1(fingerprint_string.encode('utf-8')).hexdigest()
        
        logger.info(f"Generated hardware-based node ID: {node_id[:16]}... from {len(fingerprint_parts)} hardware components")
        return node_id
    
    def get_fingerprint_summary(self) -> Dict:
        """Get a summary of the hardware fingerprint for debugging"""
        return {
            'mac_count': len(self.fingerprint_data.get('mac_addresses', [])),
            'has_system_uuid': bool(self.fingerprint_data.get('system_uuid')),
            'cpu_count': self.fingerprint_data.get('cpu_count'),
            'memory_gb': self.fingerprint_data.get('memory_gb'),
            'platform': self.fingerprint_data.get('platform', '')[:50],  # Truncate for readability
            'hostname': self.fingerprint_data.get('hostname'),
            'is_fallback': self.fingerprint_data.get('fallback', False)
        }
    
    def _sanitize_for_log(self) -> Dict:
        """Sanitize fingerprint data for logging (hide sensitive info)"""
        sanitized = self.fingerprint_data.copy()
        
        # Mask MAC addresses for privacy
        if 'mac_addresses' in sanitized:
            sanitized['mac_addresses'] = [f"{mac[:8]}:XX:XX:XX" for mac in sanitized['mac_addresses']]
        
        # Mask system UUID
        if 'system_uuid' in sanitized and sanitized['system_uuid']:
            uuid_str = sanitized['system_uuid']
            sanitized['system_uuid'] = f"{uuid_str[:8]}...{uuid_str[-4:]}"
        
        return sanitized
    
    def validate_consistency(self, stored_node_id: str, port: int = None) -> bool:
        """Validate that the stored node ID matches current hardware"""
        current_node_id = self.generate_node_id(port)
        is_consistent = stored_node_id == current_node_id
        
        if not is_consistent:
            logger.warning(f"Hardware fingerprint mismatch: stored={stored_node_id[:16]}..., current={current_node_id[:16]}...")
            logger.info("This may indicate hardware changes or first run on new hardware")
        
        return is_consistent
