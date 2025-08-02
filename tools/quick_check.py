import asyncio
from client.api import Client

async def quick_network_check():
    client = Client(bootstrap_nodes="localhost:8001")
    
    try:
        # Get all nodes
        nodes = await client.dht_discovery.get_nodes()
        print(f"🌐 Available nodes: {len(nodes)}")
        
        for node in nodes:
            print(f"  • {node.node_id[:8]}... ({node.ip}:{node.port}) - {node.model} - Load: {node.load:.2f}")
            
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(quick_network_check())
