import asyncio
from client.api import OpenAIClient

async def main():
    # Create an OpenAI-compatible client with bootstrap nodes
    client = OpenAIClient(
        bootstrap_nodes="localhost:8001",  # Connect to bootstrap node
        model="llamanet"  # Use the default model name
    )
    
    try:
        # First, show available nodes
        nodes = await client.dht_discovery.get_nodes()
        print(f"🌐 Available nodes: {len(nodes)}")
        
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
            
        # Test round robin distribution
        print("\n🔄 Testing round robin distribution...")
        for i in range(6):
            print(f"\n--- Request {i+1} ---")
            response = await client.chat_completions(
                messages=[{"role": "user", "content": f"Hello {i+1}"}],
                max_tokens=50,
                strategy="round_robin"
            )
            if response:
                print(f"Response from: {response.id}")
            
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
