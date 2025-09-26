import asyncio
from client.event_aware_client import EventAwareOpenAIClient

async def node_change_callback(event):
    """Callback for real-time node events"""
    print(f"🔔 Network event: {event.event_type.value}")
    if event.node_info:
        print(f"   Node: {event.node_info.node_id[:12]}... at {event.node_info.ip}:{event.node_info.port}")

async def main():
    # Create an event-aware OpenAI-compatible client with real-time discovery
    client = EventAwareOpenAIClient(
        bootstrap_nodes="localhost:8001",  # Connect to bootstrap node
        model="llamanet",  # Use the default model name
        event_callback=node_change_callback  # Get real-time notifications
    )
    
    try:
        # Start the client and wait for nodes
        print("🚀 Starting event-aware client...")
        await client.start()
        
        print("⏳ Waiting for nodes to be discovered...")
        nodes_found = await client.wait_for_nodes(min_nodes=1, timeout=30.0)
        
        if not nodes_found:
            print("❌ No nodes found within timeout")
            return
        
        # Show real-time network stats
        stats = client.get_real_time_stats()
        print(f"\n📊 Real-time network stats:")
        print(f"   Total nodes: {stats['total_nodes']}")
        print(f"   Network health: {stats['network_health']}")
        print(f"   Average load: {stats.get('avg_load', 0):.3f}")
        print(f"   Total capacity: {stats.get('total_capacity', 0):.2f} TPS")
        
        # Show available nodes (real-time, no polling)
        nodes = await client.get_available_nodes()
        print(f"\n🌐 Available nodes: {len(nodes)}")
        
        for node in nodes:
            print(f"  • {node.node_id[:8]}... ({node.ip}:{node.port}) - {node.model} - Load: {node.load:.2f}")
        
        # Test chat completions (OpenAI format)
        print("\n🤖 Testing chat completions...")
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is LlamaNet and how does it work?"}
        ]
        
        response = await client.chat_completions(
            messages=messages,
            max_tokens=150,
            temperature=0.7,
            strategy="round_robin"
        )
        
        if response:
            print(f"✅ Chat completion response:")
            print(f"   ID: {response.id}")
            print(f"   Model: {response.model}")
            print(f"   Content: {response.choices[0].message.content}")
            print(f"   Tokens used: {response.usage.total_tokens}")
        else:
            print("❌ No response received")
            
        # Test text completions (OpenAI format)
        print("\n📝 Testing text completions...")
        completion_response = await client.completions(
            prompt="LlamaNet is a decentralized inference network that",
            max_tokens=100,
            temperature=0.7,
            strategy="round_robin"
        )
        
        if completion_response:
            print(f"✅ Text completion response:")
            print(f"   ID: {completion_response.id}")
            print(f"   Text: {completion_response.choices[0].text}")
            print(f"   Tokens used: {completion_response.usage.total_tokens}")
        else:
            print("❌ No completion response received")
            
        # Test round robin distribution with real-time updates
        print("\n🔄 Testing round robin distribution...")
        for i in range(6):
            print(f"\n--- Request {i+1} ---")
            
            # Show current network state
            current_stats = client.get_real_time_stats()
            print(f"Current nodes: {current_stats['total_nodes']}")
            
            response = await client.chat_completions(
                messages=[{"role": "user", "content": f"Hello {i+1}"}],
                max_tokens=50,
                strategy="round_robin"
            )
            if response:
                print(f"Response from: {response.id}")
            
            # Small delay to see any real-time changes
            await asyncio.sleep(2)
            
        # Demonstrate real-time monitoring
        print("\n⏱️  Monitoring network for 10 seconds...")
        print("   (Try starting/stopping nodes to see real-time updates)")
        
        for i in range(10):
            await asyncio.sleep(1)
            stats = client.get_real_time_stats()
            print(f"   Nodes: {stats['total_nodes']}, Health: {stats['network_health']}")
            
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
