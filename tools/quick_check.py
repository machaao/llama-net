import asyncio
import requests
from client.api import Client

async def quick_network_check():
    client = Client(bootstrap_nodes="localhost:8001")
    
    try:
        # Get all nodes
        nodes = await client.dht_discovery.get_nodes()
        print(f"🌐 Available nodes: {len(nodes)}")
        
        if not nodes:
            print("❌ No nodes found in the network")
            return
        
        print("\n📊 Node Status:")
        print("-" * 60)
        
        for node in nodes:
            print(f"🔹 {node.node_id[:8]}... ({node.ip}:{node.port})")
            print(f"   Model: {node.model}")
            print(f"   Load: {node.load:.2f} | TPS: {node.tps:.2f}")
            
            # Try to get health status
            try:
                health_response = requests.get(f"http://{node.ip}:{node.port}/health", timeout=3)
                if health_response.status_code == 200:
                    health = health_response.json()
                    health_icon = "💚" if health.get('healthy', False) else "💔"
                    print(f"   Health: {health_icon} {'Healthy' if health.get('healthy', False) else 'Unhealthy'}")
                else:
                    print(f"   Health: ❓ Unknown")
            except:
                print(f"   Health: ❌ Unreachable")
            
            print()
            
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(quick_network_check())
