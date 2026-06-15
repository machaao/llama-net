#!/usr/bin/env python3
"""Test Hugging Face URL parsing"""

from inference_node.hf_downloader import HFModelDownloader

def test_parsing():
    downloader = HFModelDownloader()
    
    test_cases = [
        ("hf.co/LiquidAI/LFM2.5-1.2B-JP-202606-GGUF:Q4_K_M", 
         ("LiquidAI/LFM2.5-1.2B-JP-202606-GGUF", "Q4_K_M")),
        
        ("LiquidAI/LFM2.5-1.2B-JP-202606-GGUF:Q4_K_M",
         ("LiquidAI/LFM2.5-1.2B-JP-202606-GGUF", "Q4_K_M")),
        
        ("meta-llama/Llama-2-7b-chat-hf:Q4_K_M",
         ("meta-llama/Llama-2-7b-chat-hf", "Q4_K_M")),
        
        ("meta-llama/Llama-2-7b-chat-hf",
         ("meta-llama/Llama-2-7b-chat-hf", None)),
        
        ("https://huggingface.co/meta-llama/Llama-2-7b-chat-hf:Q4_K_M",
         ("meta-llama/Llama-2-7b-chat-hf", "Q4_K_M")),
    ]
    
    print("Testing Hugging Face URL parsing...\n")
    
    for url, expected in test_cases:
        try:
            repo_id, filename, tag = downloader.parse_hf_url(url)
            expected_repo_id, expected_tag = expected
            
            success = (repo_id == expected_repo_id and tag == expected_tag)
            status = "✅ PASS" if success else "❌ FAIL"
            
            print(f"{status}: {url}")
            print(f"  Expected: repo_id={expected_repo_id}, tag={expected_tag}")
            print(f"  Got:      repo_id={repo_id}, tag={tag}")
            print()
            
        except Exception as e:
            print(f"❌ ERROR: {url}")
            print(f"  Exception: {e}\n")

if __name__ == "__main__":
    test_parsing()
