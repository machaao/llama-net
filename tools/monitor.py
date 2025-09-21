import asyncio
import time
import os
import requests
from client.dht_discovery import DHTDiscovery

async def monitor_network(bootstrap_nodes="localhost:8001", interval=10):
    """Monitor the network in real-time"""
    discovery = DHTDiscovery(bootstrap_nodes)
    
    try:
        while True:
            os.system('clear' if os.name == 'posix' else 'cls')  # Clear screen
            
            print("🔄 LlamaNet Network Monitor")
            print(f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}")
            print("=" * 60)
            
            nodes = await discovery.get_nodes(force_refresh=True)
            
            if nodes:
                print(f"📊 Active Nodes: {len(nodes)}")
                print("-" * 60)
                
                for node in nodes:
                    status = "🟢" if time.time() - node.last_seen < 30 else "🟡"
                    print(f"{status} {node.node_id[:12]}... | {node.ip}:{node.port} | {node.model}")
                    print(f"    Load: {node.load:.2f} | TPS: {node.tps:.2f} | Uptime: {node.uptime}s")
                    print()
            else:
                print("❌ No nodes found")
            
            print(f"\n🔄 Refreshing in {interval} seconds... (Ctrl+C to exit)")
            await asyncio.sleep(interval)
            
    except KeyboardInterrupt:
        print("\n👋 Monitoring stopped")
    finally:
        await discovery.stop()

if __name__ == "__main__":
    asyncio.run(monitor_network())
