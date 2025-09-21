import asyncio
import aiohttp
import json

async def test_openai_compatibility():
    """Test OpenAI-compatible endpoints"""
    base_url = "http://localhost:8000"
    
    async with aiohttp.ClientSession() as session:
        
        # Test models endpoint
        print("🔍 Testing /v1/models endpoint...")
        async with session.get(f"{base_url}/v1/models") as response:
            if response.status == 200:
                models = await response.json()
                print(f"✅ Available models: {[m['id'] for m in models['data']]}")
            else:
                print(f"❌ Models endpoint failed: {response.status}")
                return
        
        # Test completions endpoint
        print("\n🤖 Testing /v1/completions endpoint...")
        completion_request = {
            "model": "llamanet",
            "prompt": "What is artificial intelligence?",
            "max_tokens": 100,
            "temperature": 0.7
        }
        
        async with session.post(
            f"{base_url}/v1/completions",
            json=completion_request,
            headers={"Content-Type": "application/json"}
        ) as response:
            if response.status == 200:
                completion = await response.json()
                print(f"✅ Completion response:")
                print(f"   ID: {completion['id']}")
                print(f"   Text: {completion['choices'][0]['text'][:100]}...")
                print(f"   Tokens: {completion['usage']['total_tokens']}")
            else:
                print(f"❌ Completions endpoint failed: {response.status}")
                text = await response.text()
                print(f"   Error: {text}")
        
        # Test chat completions endpoint
        print("\n💬 Testing /v1/chat/completions endpoint...")
        chat_request = {
            "model": "llamanet",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Explain quantum computing in simple terms."}
            ],
            "max_tokens": 150,
            "temperature": 0.7
        }
        
        async with session.post(
            f"{base_url}/v1/chat/completions",
            json=chat_request,
            headers={"Content-Type": "application/json"}
        ) as response:
            if response.status == 200:
                chat_completion = await response.json()
                print(f"✅ Chat completion response:")
                print(f"   ID: {chat_completion['id']}")
                print(f"   Message: {chat_completion['choices'][0]['message']['content'][:100]}...")
                print(f"   Tokens: {chat_completion['usage']['total_tokens']}")
            else:
                print(f"❌ Chat completions endpoint failed: {response.status}")
                text = await response.text()
                print(f"   Error: {text}")

def test_with_openai_library():
    """Example using the official OpenAI Python library"""
    print("\n📚 Example using OpenAI Python library:")
    print("""
# Install: pip install openai

import openai

# Configure to use LlamaNet
openai.api_base = "http://localhost:8000/v1"
openai.api_key = "dummy-key"  # Not used but required

# Text completion
response = openai.Completion.create(
    model="llamanet",
    prompt="What is machine learning?",
    max_tokens=100
)
print(response.choices[0].text)

# Chat completion
response = openai.ChatCompletion.create(
    model="llamanet",
    messages=[
        {"role": "user", "content": "Hello, how are you?"}
    ]
)
print(response.choices[0].message.content)
""")

if __name__ == "__main__":
    print("🚀 Testing OpenAI-compatible endpoints...")
    print("Make sure you have a LlamaNet node running on localhost:8000")
    print()
    
    asyncio.run(test_openai_compatibility())
    test_with_openai_library()
