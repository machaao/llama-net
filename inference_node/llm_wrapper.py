import time
from typing import Dict, List, Optional, Any, Generator
from llama_cpp import Llama
from common.utils import get_logger, normalize_stop_tokens
from inference_node.config import InferenceConfig

logger = get_logger(__name__)

class LlamaWrapper:
    """Wrapper for llama-cpp-python"""
    
    def __init__(self, config: InferenceConfig):
        self.config = config
        self.start_time = time.time()
        
        logger.info(f"Loading model from {config.model_path}")
        self.llm = Llama(
            model_path=config.model_path,
            n_ctx=config.n_ctx,
            n_batch=config.n_batch,
            n_gpu_layers=config.n_gpu_layers
        )
        logger.info(f"Model loaded successfully: {config.model_name}")
        
        # Metrics
        self.total_tokens_generated = 0
        self.total_generation_time = 0
        self.request_count = 0
        
    def generate(self, 
                prompt: str, 
                max_tokens: int = 100,
                temperature: float = 0.7,
                top_p: float = 0.9,
                top_k: int = 40,
                stop: Optional[List[str]] = None,
                repeat_penalty: float = 1.1) -> Dict[str, Any]:
        """Generate text from a prompt"""
        self.request_count += 1
        
        start_time = time.time()
        
        # Normalize stop tokens for llama-cpp-python
        stop_tokens = normalize_stop_tokens(stop)
        
        # Generate text
        output = self.llm(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=stop_tokens,
            repeat_penalty=repeat_penalty
        )
        
        generation_time = time.time() - start_time
        tokens_generated = output['usage']['completion_tokens'] if 'usage' in output else len(output['choices'][0]['text'].split())
        
        # Update metrics
        self.total_tokens_generated += tokens_generated
        self.total_generation_time += generation_time
        
        return {
            "text": output['choices'][0]['text'],
            "tokens_generated": tokens_generated,
            "generation_time": generation_time
        }
    
    def generate_stream(self, 
                       prompt: str, 
                       max_tokens: int = 100,
                       temperature: float = 0.7,
                       top_p: float = 0.9,
                       top_k: int = 40,
                       stop: Optional[List[str]] = None,
                       repeat_penalty: float = 1.1) -> Generator[Dict[str, Any], None, None]:
        """Generate text with streaming support"""
        self.request_count += 1
        start_time = time.time()
        
        # Normalize stop tokens for llama-cpp-python
        stop_tokens = normalize_stop_tokens(stop)
        
        # Create streaming generator
        stream = self.llm(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=stop_tokens,
            repeat_penalty=repeat_penalty,
            stream=True  # Enable streaming
        )
        
        total_tokens = 0
        accumulated_text = ""
        
        try:
            for chunk in stream:
                if 'choices' in chunk and len(chunk['choices']) > 0:
                    choice = chunk['choices'][0]
                    if 'text' in choice and choice['text']:
                        total_tokens += 1
                        accumulated_text += choice['text']
                        generation_time = time.time() - start_time
                        
                        yield {
                            "text": choice['text'],
                            "accumulated_text": accumulated_text,
                            "tokens_generated": total_tokens,
                            "generation_time": generation_time,
                            "finished": choice.get('finish_reason') is not None
                        }
                        
                        if choice.get('finish_reason') is not None:
                            break
        finally:
            # Update metrics
            final_time = time.time() - start_time
            self.total_tokens_generated += total_tokens
            self.total_generation_time += final_time

    async def generate_stream_async(self, 
                                   prompt: str, 
                                   max_tokens: int = 100,
                                   temperature: float = 0.7,
                                   top_p: float = 0.9,
                                   top_k: int = 40,
                                   stop: Optional[List[str]] = None,
                                   repeat_penalty: float = 1.1) -> Generator[Dict[str, Any], None, None]:
        """Async wrapper for streaming generation"""
        for chunk in self.generate_stream(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=stop,
            repeat_penalty=repeat_penalty
        ):
            yield chunk
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get metrics about the model"""
        uptime = int(time.time() - self.start_time)
        
        # Calculate tokens per second
        tps = 0
        if self.total_generation_time > 0:
            tps = self.total_tokens_generated / self.total_generation_time
            
        # Calculate load (simple implementation)
        load = min(1.0, self.request_count / 10)  # Arbitrary scale
        if self.request_count > 0:
            self.request_count -= 1  # Decay load over time
            
        return {
            "uptime": uptime,
            "tps": round(tps, 2),
            "load": round(load, 2),
            "total_tokens": self.total_tokens_generated,
            "model": self.config.model_name
        }
