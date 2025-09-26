import asyncio
import logging
import os
import sys

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def test_components():
    """Test LlamaNet components individually"""
    
    # Test 1: Basic networking
    logger.info("🔍 Testing basic UDP networking...")
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', 8001))
        sock.close()
        logger.info("✅ Basic UDP test passed")
    except Exception as e:
        logger.error(f"❌ Basic UDP test failed: {e}")
        return False
    
    # Test 2: DHT Service
    logger.info("🔍 Testing DHT service...")
    try:
        from common.dht_service import SharedDHTService
        dht_service = SharedDHTService()
        await dht_service.initialize("test-node", 8001)
        
        if dht_service.is_initialized():
            logger.info("✅ DHT service test passed")
            await dht_service.stop()
        else:
            logger.error("❌ DHT service not initialized")
            return False
            
    except Exception as e:
        logger.error(f"❌ DHT service test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 3: Configuration
    logger.info("🔍 Testing configuration...")
    try:
        # Set a dummy model path for testing
        os.environ['MODEL_PATH'] = '/tmp/dummy.gguf'
        
        from inference_node.config import InferenceConfig
        config = InferenceConfig()
        logger.info(f"✅ Configuration test passed: {config}")
        
    except Exception as e:
        logger.error(f"❌ Configuration test failed: {e}")
        return False
    
    logger.info("🎉 All component tests passed!")
    return True

if __name__ == "__main__":
    success = asyncio.run(test_components())
    sys.exit(0 if success else 1)
