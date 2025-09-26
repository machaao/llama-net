import asyncio
import time
from typing import Dict, Any
from inference_node.event_publisher import EventBasedDHTPublisher
from common.utils import get_logger

logger = get_logger(__name__)

class HardwareBasedDHTPublisher(EventBasedDHTPublisher):
    """Hardware-based DHT publisher with enhanced validation and consistency checks"""
    
    def __init__(self, config, metrics_callback):
        super().__init__(config, metrics_callback)
        
        # Additional hardware-specific tracking
        self.hardware_validation_interval = 300  # 5 minutes
        self.last_hardware_validation = 0
        self.hardware_change_count = 0
        
        logger.info(f"Hardware-based DHT publisher initialized for node: {config.node_id[:16]}...")
    
    async def start(self):
        """Start the hardware-based DHT publisher with enhanced validation"""
        # Perform initial hardware validation
        await self._perform_initial_hardware_validation()
        
        # Start the base publisher
        await super().start()
        
        logger.info("Hardware-based DHT publisher started with validated node ID")
    
    async def _perform_initial_hardware_validation(self):
        """Perform comprehensive hardware validation before starting"""
        if not self.hardware_fingerprint:
            logger.warning("Hardware fingerprint not available for validation")
            return
        
        try:
            # Check if current node ID matches hardware
            expected_node_id = self.hardware_fingerprint.generate_node_id(self.config.port)
            
            if self.config.node_id != expected_node_id:
                logger.warning("Initial hardware validation failed!")
                logger.info(f"Expected: {expected_node_id[:16]}...")
                logger.info(f"Current:  {self.config.node_id[:16]}...")
                
                # Check if we have a stored node ID that matches
                stored_node_id = self.config._get_stored_node_id() if hasattr(self.config, '_get_stored_node_id') else None
                
                if stored_node_id == expected_node_id:
                    logger.info("Found matching stored node ID, updating configuration")
                    self.config.node_id = expected_node_id
                elif stored_node_id and stored_node_id != expected_node_id:
                    logger.warning("Stored node ID also doesn't match current hardware")
                    logger.info("This indicates significant hardware changes")
                    
                    # Update to new hardware-based ID
                    self.config.node_id = expected_node_id
                    if hasattr(self.config, '_store_node_id'):
                        self.config._store_node_id(expected_node_id)
                    
                    self.hardware_change_count += 1
                    logger.info(f"Updated to new hardware-based node ID: {expected_node_id[:16]}...")
            else:
                logger.info("Initial hardware validation passed")
                
        except Exception as e:
            logger.error(f"Error during initial hardware validation: {e}")
    
    async def _monitor_changes(self):
        """Enhanced monitoring with hardware validation"""
        while self.running:
            try:
                current_metrics = self.metrics_callback()
                current_time = time.time()
                
                # Check if metrics changed significantly
                should_update = self._should_update_metrics(current_metrics)
                
                # Force update periodically
                if current_time - self.last_forced_update > self.forced_update_interval:
                    should_update = True
                    self.last_forced_update = current_time
                    logger.debug("Forcing periodic DHT update")
                
                # Periodic hardware validation
                if current_time - self.last_hardware_validation > self.hardware_validation_interval:
                    await self._validate_hardware_consistency()
                    self.last_hardware_validation = current_time
                
                if should_update:
                    await self._publish_node_info()
                    self.last_published_metrics = current_metrics.copy()
                
                await asyncio.sleep(5)  # Check every 5 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error monitoring changes: {e}")
                await asyncio.sleep(10)
    
    async def _validate_hardware_consistency(self):
        """Periodic hardware consistency validation"""
        if not self.hardware_fingerprint:
            return
        
        try:
            # Regenerate fingerprint to check for changes
            from common.hardware_fingerprint import HardwareFingerprint
            fresh_fingerprint = HardwareFingerprint()
            
            current_node_id = fresh_fingerprint.generate_node_id(self.config.port)
            
            if current_node_id != self.config.node_id:
                logger.warning("Hardware consistency check failed!")
                logger.info("Hardware may have changed during runtime")
                
                # Handle the hardware change
                await self.handle_hardware_change()
                
                # Update our fingerprint reference
                self.hardware_fingerprint = fresh_fingerprint
                self.hardware_change_count += 1
            else:
                logger.debug("Periodic hardware consistency check passed")
                
        except Exception as e:
            logger.error(f"Error during hardware consistency validation: {e}")
    
    def get_hardware_stats(self) -> Dict[str, Any]:
        """Get hardware-related statistics"""
        stats = self.get_node_info()
        
        stats.update({
            'hardware_validation_interval': self.hardware_validation_interval,
            'last_hardware_validation': self.last_hardware_validation,
            'hardware_change_count': self.hardware_change_count,
            'validation_enabled': self.hardware_fingerprint is not None
        })
        
        return stats

# For backward compatibility, alias the event-based version as DHTPublisher
DHTPublisher = HardwareBasedDHTPublisher
