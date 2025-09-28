import time
import asyncio
from typing import Dict, List, Optional, Any, Generator
from llama_cpp import Llama
from common.utils import get_logger, normalize_stop_tokens
from common.metrics_manager import MetricsManager
from common.error_handler import ErrorHandler
from inference_node.config import InferenceConfig

logger = get_logger(__name__)

def detect_chat_format_from_model_name(model_name: str) -> str:
    """
    Detect appropriate chat format based on model name patterns.
    Returns the best matching chat format or 'chatml' as fallback.
    """
    model_name_lower = model_name.lower()
    
    # Model name to chat format mapping (ordered by specificity)
    format_patterns = {
        'llama-3': ['llama-3', 'llama3', 'meta-llama-3'],
        'llama-2': ['llama-2', 'llama2', 'meta-llama-2'],
        'mistral-instruct': ['mistral-instruct', 'mistral-7b-instruct', 'mixtral-instruct'],
        'gemma': ['gemma', 'google/gemma'],
        'zephyr': ['zephyr', 'huggingfaceh4/zephyr'],
        'openchat': ['openchat', 'openchat-3'],
        'functionary-v2': ['functionary-v2'],
        'functionary-v1': ['functionary-v1'],
        'functionary': ['functionary', 'meetkai/functionary'],
        'chatglm3': ['chatglm3', 'chatglm-3'],
        'qwen': ['qwen-', 'qwen1.5', 'alibaba/qwen'],
        'baichuan-2': ['baichuan-2', 'baichuan2'],
        'baichuan': ['baichuan'],
        'phind': ['phind', 'phind-codellama'],
        'intel': ['neural-chat', 'intel/neural-chat'],
        'saiga': ['saiga', 'ilyagusev/saiga'],
        'pygmalion': ['pygmalion', 'pygmalion-'],
        'open-orca': ['open-orca', 'openorca'],
        'redpajama-incite': ['redpajama-incite', 'togethercomputer/redpajama'],
        'snoozy': ['snoozy'],
        'openbuddy': ['openbuddy', 'openbuddy-'],
        'oasst_llama': ['oasst', 'openassistant'],
        'mistrallite': ['mistrallite', 'amazon/mistrallite'],
        'chatml-function-calling': ['chatml-function-calling'],
        'vicuna': ['vicuna'],
        'alpaca': ['alpaca', 'wizard'],
        'chatml': ['chatml', 'openai', 'oss', 'gpt', 'yi-', 'qwen2']  # Keep chatml patterns last as fallback
    }
    
    # Check each format pattern (order matters for specificity)
    for chat_format, patterns in format_patterns.items():
        for pattern in patterns:
            if pattern in model_name_lower:
                logger.info(f"Detected chat format '{chat_format}' for model '{model_name}' (matched pattern: '{pattern}')")
                return chat_format
    
    # Fallback to chatml for unknown models
    logger.info(f"No specific chat format detected for model '{model_name}', using 'chatml' as fallback")
    return 'chatml'

class LlamaWrapper:
    """Wrapper for llama-cpp-python with chat template support"""
    
    def __init__(self, config: InferenceConfig):
        self.config = config
        self.metrics_manager = MetricsManager()
        
        # Add processing lock for thread safety
        self._processing_lock = asyncio.Lock()
        
        # Auto-detect chat format based on model name
        self.detected_chat_format = detect_chat_format_from_model_name(config.model_name)
        
        logger.info(f"Loading model from {config.model_path}")
        logger.info(f"Using detected chat format: {self.detected_chat_format}")
        
        self.llm = Llama(
            model_path=config.model_path,
            n_ctx=config.n_ctx,
            n_batch=config.n_batch,
            n_gpu_layers=config.n_gpu_layers,
            verbose=config.verbose,
            reasoning=True,
            chat_format=self.detected_chat_format  # Use detected format instead of "auto"
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
    
    def is_processing(self) -> bool:
        """Check if the LLM is currently processing a request"""
        return self._processing_lock.locked()
        
    async def generate_chat_safe(self, *args, **kwargs) -> Dict[str, Any]:
        """Thread-safe version of generate_chat"""
        async with self._processing_lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.generate_chat, *args, **kwargs)
            
    async def generate_safe(self, *args, **kwargs) -> Dict[str, Any]:
        """Thread-safe version of generate"""
        async with self._processing_lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.generate, *args, **kwargs)
            
    async def generate_chat_stream_safe(self, *args, **kwargs) -> Generator[Dict[str, Any], None, None]:
        """Thread-safe version of generate_chat_stream with content filtering"""
        async with self._processing_lock:
            loop = asyncio.get_event_loop()
            
            def create_stream():
                return self.generate_chat_stream(*args, **kwargs)
            
            # Create the generator in a thread
            stream_gen = await loop.run_in_executor(None, create_stream)
            
            message_started = False
            
            # Yield chunks from the generator in thread executor
            def get_next_chunk():
                try:
                    return next(stream_gen)
                except StopIteration:
                    return None
            
            while True:
                chunk = await loop.run_in_executor(None, get_next_chunk)
                if chunk is None:
                    break
                
                text = chunk.get("text", "")
                
                if text:
                    # Check for message start marker
                    if not message_started and "<|message|>" in text:
                        message_started = True
                        # Extract content after marker
                        actual_text = self._extract_actual_response(text)
                        if actual_text:
                            yield {
                                "text": actual_text,
                                "accumulated_text": chunk.get("accumulated_text", ""),
                                "tokens_generated": chunk.get("tokens_generated", 0),
                                "generation_time": chunk.get("generation_time", 0),
                                "finished": chunk.get("finished", False)
                            }
                    elif message_started:
                        # We're in actual content, pass through
                        yield chunk
                    elif self._should_filter_content(text):
                        # Filter out reasoning/special tokens
                        continue
                    else:
                        # Pass through other content
                        yield chunk
                else:
                    yield chunk
                
                if chunk.get("finished"):
                    break
                
    async def generate_stream_safe(self, *args, **kwargs) -> Generator[Dict[str, Any], None, None]:
        """Thread-safe version of generate_stream"""
        async with self._processing_lock:
            loop = asyncio.get_event_loop()
            
            def create_stream():
                return self.generate_stream(*args, **kwargs)
            
            # Create the generator in a thread
            stream_gen = await loop.run_in_executor(None, create_stream)
            
            # Yield chunks from the generator in thread executor
            def get_next_chunk():
                try:
                    return next(stream_gen)
                except StopIteration:
                    return None
            
            while True:
                chunk = await loop.run_in_executor(None, get_next_chunk)
                if chunk is None:
                    break
                yield chunk

    def _should_filter_content(self, text: str) -> bool:
        """Check if content should be filtered out (reasoning/special tokens)"""
        if not text:
            return False
        
        # Filter out reasoning markers and special tokens
        filter_patterns = [
            "<|end|>", "<|start|>", "<|channel|>", 
            "assistant", "final", "user says", "instruction:",
            "We need to respond", "The conversation:", "Let's answer"
        ]
        
        # If text contains only filter patterns, filter it out
        for pattern in filter_patterns:
            if pattern in text and len(text.strip()) < 50:  # Short text with markers
                return True
        
        return False

    def _extract_actual_response(self, text: str) -> str:
        """Extract actual response content after message marker"""
        if "<|message|>" in text:
            marker_idx = text.find("<|message|>")
            return text[marker_idx + len("<|message|>"):]
        return text

    def get_chat_template_info(self) -> Dict[str, Any]:
        """Get information about the current chat template"""
        try:
            info = {
                "chat_format": getattr(self.llm, 'chat_format', self.detected_chat_format),
                "detected_format": self.detected_chat_format,
                "supports_chat": True,
                "template_auto_detected": True,
                "supported_roles": ["system", "user", "assistant"],
                "available_formats": [
                    'llama-2', 'llama-3', 'alpaca', 'qwen', 'vicuna', 'oasst_llama', 
                    'baichuan-2', 'baichuan', 'openbuddy', 'redpajama-incite', 'snoozy', 
                    'phind', 'intel', 'open-orca', 'mistrallite', 'zephyr', 'pygmalion', 
                    'chatml', 'mistral-instruct', 'chatglm3', 'openchat', 'saiga', 'gemma', 
                    'functionary', 'functionary-v2', 'functionary-v1', 'chatml-function-calling'
                ]
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
                     repeat_penalty: float = 1.1,
                     reasoning: bool = True) -> Dict[str, Any]:
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
            
            # Extract response with reasoning support
            response_content = ""
            reasoning_content = ""
            tokens_generated = 0
            
            if 'choices' in output and len(output['choices']) > 0:
                choice = output['choices'][0]
                if 'message' in choice and 'content' in choice['message']:
                    response_content = choice['message']['content']
                    
                    # Extract reasoning if present (for reasoning models)
                    if reasoning and hasattr(choice['message'], 'reasoning'):
                        reasoning_content = choice['message'].get('reasoning', '')
                
            if 'usage' in output:
                tokens_generated = output['usage'].get('completion_tokens', 0)
            else:
                tokens_generated = len(response_content.split())
            
            result = {
                "text": response_content,
                "tokens_generated": tokens_generated,
                "generation_time": generation_time
            }
            
            if reasoning_content:
                result["reasoning"] = reasoning_content
                
            return result
            
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
                           repeat_penalty: float = 1.1,
                           reasoning: bool = True) -> Generator[Dict[str, Any], None, None]:
        """Generate streaming chat completion with content filtering"""
        self.metrics_manager.record_request_start()
        start_time = time.time()
        
        # Prepare stop tokens - don't add reasoning markers as stop tokens
        # since we want to detect them for filtering
        stop_tokens = normalize_stop_tokens(stop)
        
        # Create streaming generator
        stream = self.llm.create_chat_completion(
            messages=self._format_messages(messages),
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=stop_tokens,
            repeat_penalty=repeat_penalty,
            stream=True
        )
        
        total_tokens = 0
        accumulated_text = ""
        message_content_started = False
        content_buffer = ""
        
        try:
            for chunk in stream:
                if 'choices' in chunk and len(chunk['choices']) > 0:
                    choice = chunk['choices'][0]
                    delta = choice.get('delta', {})
                    
                    chunk_data = {}
                    
                    if 'content' in delta and delta['content']:
                        content = delta['content']
                        
                        if not message_content_started:
                            # Buffer content until we find the message marker
                            content_buffer += content
                            
                            # Check if we've found the message start marker
                            if "<|message|>" in content_buffer:
                                message_content_started = True
                                # Extract content after the marker
                                marker_idx = content_buffer.find("<|message|>")
                                actual_content = content_buffer[marker_idx + len("<|message|>"):]
                                
                                if actual_content:
                                    total_tokens += len(actual_content.split())
                                    accumulated_text += actual_content
                                    chunk_data["text"] = actual_content
                                    chunk_data["accumulated_text"] = accumulated_text
                            
                            # Skip yielding until we find the marker
                            if not message_content_started:
                                continue
                        else:
                            # We're in actual message content
                            total_tokens += 1
                            accumulated_text += content
                            chunk_data["text"] = content
                            chunk_data["accumulated_text"] = accumulated_text
                    
                    # Handle reasoning content if present (but don't include in main response)
                    if reasoning and 'reasoning' in delta and delta['reasoning']:
                        chunk_data["reasoning"] = delta['reasoning']
                    
                    if chunk_data and message_content_started:
                        generation_time = time.time() - start_time
                        chunk_data.update({
                            "tokens_generated": total_tokens,
                            "generation_time": generation_time,
                            "finished": choice.get('finish_reason') is not None
                        })
                        
                        yield chunk_data
                        
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
                                       repeat_penalty: float = 1.1,
                                       reasoning: bool = True) -> Generator[Dict[str, Any], None, None]:
        """Async wrapper for streaming chat generation"""
        for chunk in self.generate_chat_stream(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=stop,
            repeat_penalty=repeat_penalty,
            reasoning=reasoning
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
