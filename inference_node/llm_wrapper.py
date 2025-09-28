import time
from typing import Dict, List, Optional, Any, Generator
from llama_cpp import Llama
from common.utils import get_logger, normalize_stop_tokens
from common.metrics_manager import MetricsManager
from common.error_handler import ErrorHandler
from inference_node.config import InferenceConfig

logger = get_logger(__name__)

class LlamaWrapper:
    """Wrapper for llama-cpp-python with chat template support"""
    
    def __init__(self, config: InferenceConfig):
        self.config = config
        self.metrics_manager = MetricsManager()
        
        logger.info(f"Loading model from {config.model_path}")
        self.llm = Llama(
            model_path=config.model_path,
            n_ctx=config.n_ctx,
            n_batch=config.n_batch,
            n_gpu_layers=config.n_gpu_layers,
            chat_format="auto"  # Auto-detect chat format from model
        )
        
        # Detect and log the chat template being used
        self._detect_chat_template()
        logger.info(f"Model loaded successfully: {config.model_name}")
        
    def _detect_chat_template(self):
        """Detect and log the chat template being used"""
        try:
            # Check chat format
            if hasattr(self.llm, 'chat_format') and self.llm.chat_format:
                logger.info(f"Using chat format: {self.llm.chat_format}")
            else:
                logger.info("Using default chat formatting")
                
            # Try to get additional template info
            if hasattr(self.llm, '_chat_handler') and self.llm._chat_handler:
                handler_type = type(self.llm._chat_handler).__name__
                logger.info(f"Chat handler: {handler_type}")
                
        except Exception as e:
            logger.warning(f"Could not detect chat template: {e}")
    
    def _format_messages(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Format messages for llama-cpp-python"""
        formatted = []
        for msg in messages:
            formatted.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", "")
            })
        return formatted
    
    def get_chat_template_info(self) -> Dict[str, Any]:
        """Get information about the current chat template"""
        try:
            info = {
                "chat_format": getattr(self.llm, 'chat_format', 'unknown'),
                "supports_chat": True,
                "template_auto_detected": True,
                "supported_roles": ["system", "user", "assistant"]
            }
            
            if hasattr(self.llm, '_chat_handler') and self.llm._chat_handler:
                handler_type = type(self.llm._chat_handler).__name__
                info["handler_type"] = handler_type
                
            return info
        except Exception as e:
            logger.error(f"Error getting chat template info: {e}")
            return {"error": str(e)}
    
    def generate_chat(self,
                     messages: List[Dict[str, str]],
                     max_tokens: int = 100,
                     temperature: float = 0.7,
                     top_p: float = 0.9,
                     top_k: int = 40,
                     stop: Optional[List[str]] = None,
                     repeat_penalty: float = 1.1) -> Dict[str, Any]:
        """Generate chat completion using proper chat formatting"""
        self.metrics_manager.record_request_start()
        start_time = time.time()
        
        try:
            # Use create_chat_completion for proper template handling
            output = self.llm.create_chat_completion(
                messages=self._format_messages(messages),
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                stop=normalize_stop_tokens(stop),
                repeat_penalty=repeat_penalty
            )
            
            generation_time = time.time() - start_time
            
            # Extract response
            response_content = ""
            tokens_generated = 0
            
            if 'choices' in output and len(output['choices']) > 0:
                choice = output['choices'][0]
                if 'message' in choice and 'content' in choice['message']:
                    response_content = choice['message']['content']
                
            if 'usage' in output:
                tokens_generated = output['usage'].get('completion_tokens', 0)
            else:
                tokens_generated = len(response_content.split())
            
            return {
                "text": response_content,
                "tokens_generated": tokens_generated,
                "generation_time": generation_time
            }
            
        finally:
            generation_time = time.time() - start_time
            tokens_generated = output.get('usage', {}).get('completion_tokens', 0) if 'output' in locals() else 0
            self.metrics_manager.record_request_end(tokens_generated, generation_time)
    
    def generate_chat_stream(self,
                           messages: List[Dict[str, str]],
                           max_tokens: int = 100,
                           temperature: float = 0.7,
                           top_p: float = 0.9,
                           top_k: int = 40,
                           stop: Optional[List[str]] = None,
                           repeat_penalty: float = 1.1) -> Generator[Dict[str, Any], None, None]:
        """Generate streaming chat completion"""
        self.metrics_manager.record_request_start()
        start_time = time.time()
        
        # Create streaming generator
        stream = self.llm.create_chat_completion(
            messages=self._format_messages(messages),
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=normalize_stop_tokens(stop),
            repeat_penalty=repeat_penalty,
            stream=True
        )
        
        total_tokens = 0
        accumulated_text = ""
        
        try:
            for chunk in stream:
                if 'choices' in chunk and len(chunk['choices']) > 0:
                    choice = chunk['choices'][0]
                    delta = choice.get('delta', {})
                    
                    if 'content' in delta and delta['content']:
                        total_tokens += 1
                        accumulated_text += delta['content']
                        generation_time = time.time() - start_time
                        
                        yield {
                            "text": delta['content'],
                            "accumulated_text": accumulated_text,
                            "tokens_generated": total_tokens,
                            "generation_time": generation_time,
                            "finished": choice.get('finish_reason') is not None
                        }
                        
                        if choice.get('finish_reason') is not None:
                            break
        finally:
            final_time = time.time() - start_time
            self.metrics_manager.record_request_end(total_tokens, final_time)

    async def generate_chat_stream_async(self,
                                       messages: List[Dict[str, str]],
                                       max_tokens: int = 100,
                                       temperature: float = 0.7,
                                       top_p: float = 0.9,
                                       top_k: int = 40,
                                       stop: Optional[List[str]] = None,
                                       repeat_penalty: float = 1.1) -> Generator[Dict[str, Any], None, None]:
        """Async wrapper for streaming chat generation"""
        for chunk in self.generate_chat_stream(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=stop,
            repeat_penalty=repeat_penalty
        ):
            yield chunk
        
    def generate(self, 
                prompt: str, 
                max_tokens: int = 100,
                temperature: float = 0.7,
                top_p: float = 0.9,
                top_k: int = 40,
                stop: Optional[List[str]] = None,
                repeat_penalty: float = 1.1) -> Dict[str, Any]:
        """Generate text from a prompt"""
        self.metrics_manager.record_request_start()
        start_time = time.time()
        
        try:
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
            
            return {
                "text": output['choices'][0]['text'],
                "tokens_generated": tokens_generated,
                "generation_time": generation_time
            }
        finally:
            generation_time = time.time() - start_time
            tokens_generated = output['usage']['completion_tokens'] if 'output' in locals() and 'usage' in output else 0
            self.metrics_manager.record_request_end(tokens_generated, generation_time)
    
    def generate_stream(self, 
                       prompt: str, 
                       max_tokens: int = 100,
                       temperature: float = 0.7,
                       top_p: float = 0.9,
                       top_k: int = 40,
                       stop: Optional[List[str]] = None,
                       repeat_penalty: float = 1.1) -> Generator[Dict[str, Any], None, None]:
        """Generate text with streaming support"""
        self.metrics_manager.record_request_start()
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
            # Update metrics using consolidated manager
            final_time = time.time() - start_time
            self.metrics_manager.record_request_end(total_tokens, final_time)

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
        """Get metrics about the model using consolidated manager"""
        metrics = self.metrics_manager.get_comprehensive_metrics()
        metrics["model"] = self.config.model_name
        
        # Add chat template info
        template_info = self.get_chat_template_info()
        metrics["chat_template"] = template_info
        
        return metrics
