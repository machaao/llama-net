import asyncio
import requests
import json
from client.dht_discovery import DHTDiscovery

async def show_network_status(bootstrap_nodes="localhost:8001"):
    """Show the status of the entire LlamaNet network"""
    
    print("🔍 Discovering LlamaNet network...")
    
    # Create DHT discovery client
    discovery = DHTDiscovery(bootstrap_nodes)
    
    try:
        # Get all nodes
        nodes = await discovery.get_nodes()
        
        print(f"\n📊 Found {len(nodes)} active nodes:")
        print("-" * 80)
        
        for i, node in enumerate(nodes, 1):
            print(f"{i}. Node ID: {node.node_id}")
            print(f"   Address: {node.ip}:{node.port}")
            print(f"   Model: {node.model}")
            print(f"   Load: {node.load:.2f}")
            print(f"   TPS: {node.tps:.2f}")
            print(f"   Uptime: {node.uptime}s")
            print(f"   Last seen: {node.last_seen}")
            
            # Try to get additional info from HTTP API
            try:
                # Get basic info
                response = requests.get(f"http://{node.ip}:{node.port}/info", timeout=5)
                if response.status_code == 200:
                    info = response.json()
                    print(f"   DHT Port: {info.get('dht_port', 'unknown')}")
                    if 'system' in info:
                        system = info['system']
                        print(f"   CPU: {system.get('cpu', 'unknown')}")
                        if system.get('gpu'):
                            print(f"   GPU: {system['gpu']}")
                
                # Get health status
                health_response = requests.get(f"http://{node.ip}:{node.port}/health", timeout=5)
                if health_response.status_code == 200:
                    health = health_response.json()
                    health_icon = "💚" if health.get('healthy', False) else "💔"
                    print(f"   Health: {health_icon} {'Healthy' if health.get('healthy', False) else 'Unhealthy'}")
                    
            except:
                print(f"   Status: HTTP API not reachable")
            
            print()
            
    except Exception as e:
        print(f"❌ Error discovering network: {e}")
    finally:
        await discovery.stop()

if __name__ == "__main__":
    import sys
    bootstrap = sys.argv[1] if len(sys.argv) > 1 else "localhost:8001"
    asyncio.run(show_network_status(bootstrap))
