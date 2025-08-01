import time
import threading
import requests
from typing import Dict, Any, Optional
import json
from common.utils import get_logger, get_host_ip
from common.models import NodeInfo
from inference_node.config import InferenceConfig

logger = get_logger(__name__)

class HeartbeatSender:
    """Send heartbeats to the registry"""
    
    def __init__(self, config: InferenceConfig, metrics_callback):
        self.config = config
        self.metrics_callback = metrics_callback
        self.running = False
        self.thread = None
        self.node_info = NodeInfo(
            node_id=config.node_id,
            ip=get_host_ip(),
            port=config.port,
            model=config.model_name
        )
    
    def start(self):
        """Start sending heartbeats"""
        if self.running:
            return
            
        self.running = True
        self.thread = threading.Thread(target=self._heartbeat_loop)
        self.thread.daemon = True
        self.thread.start()
        logger.info(f"Started heartbeat to registry at {self.config.registry_url}")
    
    def stop(self):
        """Stop sending heartbeats"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)
            
    def _heartbeat_loop(self):
        """Send heartbeats at regular intervals"""
        while self.running:
            try:
                self._send_heartbeat()
            except Exception as e:
                logger.error(f"Failed to send heartbeat: {e}")
                
            time.sleep(self.config.heartbeat_interval)
    
    def _send_heartbeat(self):
        """Send a single heartbeat to the registry"""
        # Get current metrics
        metrics = self.metrics_callback()
        
        # Update node info
        self.node_info.load = metrics["load"]
        self.node_info.tps = metrics["tps"]
        self.node_info.uptime = metrics["uptime"]
        self.node_info.last_seen = int(time.time())
        
        # Send to registry
        response = requests.post(
            f"{self.config.registry_url}/register",
            data=self.node_info.json(),
            headers={"Content-Type": "application/json"}
        )
        
        if response.status_code != 200:
            logger.warning(f"Registry returned status {response.status_code}: {response.text}")
