import time
import asyncio
from typing import Dict, List, Optional, Any, Generator
from llama_cpp import Llama
from common.utils import get_logger, normalize_stop_tokens
from common.metrics_manager import MetricsManager
from common.error_handler import ErrorHandler
from inference_node.config import InferenceConfig

logger = get_logger(__name__)

def detect_reasoning_model(model_name: str) -> bool:
    """
    Detect if the model supports reasoning based on model name patterns.
    """
    model_name_lower = model_name.lower()
    
    reasoning_patterns = [
        'deepseek-r1', 'deepseek-reasoning', 'qwen-reasoning', 
        'reasoning', 'r1-', '-r1', 'think', 'cot', 'gpt-oss'  # Add gpt-oss pattern
    ]
    
    for pattern in reasoning_patterns:
        if pattern in model_name_lower:
            logger.info(f"Detected reasoning model: {model_name} (pattern: {pattern})")
            return True
    
    return False

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
        
        # Detect if this is a reasoning model
        self.supports_reasoning = detect_reasoning_model(config.model_name)
        logger.info(f"Reasoning support: {'enabled' if self.supports_reasoning else 'disabled'}")
        
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
    
    def _separate_reasoning_and_content(self, text: str) -> tuple[str, str]:
        """Separate reasoning content from final response"""
        if not self.supports_reasoning:
            return "", text
        
        # Look for the message marker that separates reasoning from final content
        if "<|message|>" in text:
            parts = text.split("<|message|>", 1)
            reasoning_part = parts[0].strip()
            content_part = parts[1].strip() if len(parts) > 1 else ""
            
            # Clean up reasoning part - remove special tokens
            reasoning_part = self._clean_reasoning_content(reasoning_part)
            
            return reasoning_part, content_part
        else:
            # If no marker found, treat everything as content for now
            return "", text
    
    def _clean_reasoning_content(self, reasoning_text: str) -> str:
        """Clean up reasoning content by removing special tokens"""
        if not reasoning_text:
            return ""
        
        # Remove common special tokens from reasoning
        tokens_to_remove = [
            "<|start|>", "<|end|>", "<|channel|>", "assistant", "final"
        ]
        
        cleaned = reasoning_text
        for token in tokens_to_remove:
            cleaned = cleaned.replace(token, "")
        
        # Clean up extra whitespace and newlines
        cleaned = " ".join(cleaned.split())
        
        return cleaned.strip()
    
    def is_processing(self) -> bool:
        """Check if the LLM is currently processing a request"""
        return self._processing_lock.locked()
        
    async def generate_chat_safe(self, messages, max_tokens=100, temperature=0.7, top_p=0.9, 
                               top_k=40, stop=None, repeat_penalty=1.1, reasoning=True) -> Dict[str, Any]:
        """Thread-safe version of generate_chat"""
        async with self._processing_lock:
            loop = asyncio.get_event_loop()
            
            # Validate and format arguments before passing to executor
            try:
                # Ensure messages is a list of dicts with proper format
                if not isinstance(messages, list):
                    raise ValueError("Messages must be a list")
                
                formatted_messages = []
                for msg in messages:
                    if isinstance(msg, dict):
                        formatted_messages.append({
                            "role": str(msg.get("role", "user")),
                            "content": str(msg.get("content", ""))
                        })
                    else:
                        raise ValueError(f"Invalid message format: {type(msg)}")
                
                # Use a lambda to properly pass all arguments including reasoning
                return await loop.run_in_executor(
                    None, 
                    lambda: self.generate_chat(
                        messages=formatted_messages,  # Use formatted messages
                        max_tokens=int(max_tokens),
                        temperature=float(temperature),
                        top_p=float(top_p),
                        top_k=int(top_k),
                        stop=stop,
                        repeat_penalty=float(repeat_penalty),
                        reasoning=bool(reasoning)
                    )
                )
            except Exception as e:
                logger.error(f"Error in generate_chat_safe argument preparation: {e}")
                raise
            
    async def generate_safe(self, prompt, max_tokens=100, temperature=0.7, top_p=0.9, 
                           top_k=40, stop=None, repeat_penalty=1.1) -> Dict[str, Any]:
        """Thread-safe version of generate"""
        async with self._processing_lock:
            loop = asyncio.get_event_loop()
            
            return await loop.run_in_executor(
                None,
                lambda: self.generate(
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    top_k=top_k,
                    stop=stop,
                    repeat_penalty=repeat_penalty
                )
            )
            
    async def generate_chat_stream_safe(self, *args, **kwargs) -> Generator[Dict[str, Any], None, None]:
        """Thread-safe version of generate_chat_stream"""
        async with self._processing_lock:
            loop = asyncio.get_event_loop()
            
            def create_stream():
                return self.generate_chat_stream(*args, **kwargs)
            
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
        """Generate chat completion with reasoning content separation"""
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
            
            # Extract response with reasoning separation
            response_content = ""
            reasoning_content = ""
            tokens_generated = 0
            
            if 'choices' in output and len(output['choices']) > 0:
                choice = output['choices'][0]
                if 'message' in choice and 'content' in choice['message']:
                    full_response = choice['message']['content']
                    
                    # Separate reasoning from content if reasoning is enabled
                    if self.supports_reasoning and reasoning:
                        reasoning_content, response_content = self._separate_reasoning_and_content(full_response)
                    else:
                        response_content = full_response
                
            if 'usage' in output:
                tokens_generated = output['usage'].get('completion_tokens', 0)
            else:
                tokens_generated = len(response_content.split())
            
            result = {
                "text": response_content,
                "content": response_content,
                "tokens_generated": tokens_generated,
                "generation_time": generation_time
            }
            
            # Add reasoning content if available
            if reasoning_content:
                result["reasoning"] = reasoning_content
                result["reasoning_content"] = reasoning_content
                
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
        """Generate streaming chat completion with reasoning content separation"""
        self.metrics_manager.record_request_start()
        start_time = time.time()
        
        # Prepare stop tokens
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
        reasoning_buffer = ""
        content_buffer = ""
        in_reasoning_phase = True
        message_marker_found = False
        
        try:
            for chunk in stream:
                if 'choices' in chunk and len(chunk['choices']) > 0:
                    choice = chunk['choices'][0]
                    delta = choice.get('delta', {})
                    
                    if 'content' in delta and delta['content']:
                        content = delta['content']
                        total_tokens += 1
                        accumulated_text += content
                        
                        # Check for message marker
                        if self.supports_reasoning and reasoning and "<|message|>" in accumulated_text and not message_marker_found:
                            message_marker_found = True
                            in_reasoning_phase = False
                            
                            # Split the accumulated text at the marker
                            reasoning_part, content_part = self._separate_reasoning_and_content(accumulated_text)
                            
                            # Send reasoning content if we have it
                            if reasoning_part:
                                yield {
                                    "reasoning_content": reasoning_part,
                                    "tokens_generated": total_tokens,
                                    "generation_time": time.time() - start_time,
                                    "finished": False,
                                    "reasoning_phase": True
                                }
                            
                            # Send content part if we have it
                            if content_part:
                                content_buffer = content_part
                                yield {
                                    "text": content_part,
                                    "content": content_part,
                                    "accumulated_text": content_part,
                                    "tokens_generated": total_tokens,
                                    "generation_time": time.time() - start_time,
                                    "finished": False,
                                    "reasoning_phase": False
                                }
                        
                        elif not in_reasoning_phase:
                            # We're in content phase, send content normally
                            content_buffer += content
                            yield {
                                "text": content,
                                "content": content,
                                "accumulated_text": content_buffer,
                                "tokens_generated": total_tokens,
                                "generation_time": time.time() - start_time,
                                "finished": False,
                                "reasoning_phase": False
                            }
                        
                        elif in_reasoning_phase and self.supports_reasoning and reasoning:
                            # We're still in reasoning phase, buffer the content
                            reasoning_buffer += content
                            # Don't yield reasoning chunks individually to avoid UI clutter
                        
                        else:
                            # No reasoning support or reasoning disabled, send as regular content
                            yield {
                                "text": content,
                                "content": content,
                                "accumulated_text": accumulated_text,
                                "tokens_generated": total_tokens,
                                "generation_time": time.time() - start_time,
                                "finished": False,
                                "reasoning_phase": False
                            }
                        
                        if choice.get('finish_reason') is not None:
                            # Handle case where we finish in reasoning phase without finding marker
                            if in_reasoning_phase and self.supports_reasoning and reasoning and reasoning_buffer:
                                cleaned_reasoning = self._clean_reasoning_content(reasoning_buffer)
                                if cleaned_reasoning:
                                    yield {
                                        "reasoning_content": cleaned_reasoning,
                                        "tokens_generated": total_tokens,
                                        "generation_time": time.time() - start_time,
                                        "finished": False,
                                        "reasoning_phase": True
                                    }
                            
                            # Send final chunk
                            yield {
                                "text": "",
                                "tokens_generated": total_tokens,
                                "generation_time": time.time() - start_time,
                                "finished": True,
                                "reasoning_phase": False
                            }
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
