import time
import asyncio
import base64
import io
from typing import Dict, Any, Optional, List
from PIL import Image

try:
    from stable_diffusion_cpp import StableDiffusion
    SD_AVAILABLE = True
except ImportError:
    SD_AVAILABLE = False
    StableDiffusion = None

from common.utils import get_logger
from common.metrics_manager import MetricsManager
from inference_node.sd_config import SDConfig

logger = get_logger(__name__)

class SDWrapper:
    """Wrapper for stable-diffusion-cpp-python with metrics tracking"""
    
    def __init__(self, config: SDConfig):
        if not SD_AVAILABLE:
            raise ImportError(
                "stable-diffusion-cpp-python is not installed. "
                "Install it with: pip install stable-diffusion-cpp-python"
            )
        
        self.config = config
        self.metrics_manager = MetricsManager()
        
        # Add processing lock for thread safety
        self._processing_lock = asyncio.Lock()
        
        logger.info(f"Loading SD model from {config.model_path}")
        logger.info(f"Model type: {config.model_type}")
        
        # Initialize StableDiffusion with configuration
        init_params = {
            'model_path': config.model_path,
            'wtype': config.wtype,
            'n_threads': config.n_threads if config.n_threads > 0 else -1,
            'vae_tiling': config.vae_tiling,
            'free_params_immediately': config.free_params_immediately
        }
        
        # Add optional paths if provided
        if config.vae_path:
            init_params['vae_path'] = config.vae_path
            logger.info(f"Using VAE: {config.vae_path}")
        
        if config.taesd_path:
            init_params['taesd_path'] = config.taesd_path
            logger.info(f"Using TAESD: {config.taesd_path}")
        
        if config.control_net_path:
            init_params['control_net_path'] = config.control_net_path
            logger.info(f"Using ControlNet: {config.control_net_path}")
        
        if config.lora_model_dir:
            init_params['lora_model_dir'] = config.lora_model_dir
            logger.info(f"Using LoRA directory: {config.lora_model_dir}")
        
        if config.embeddings_path:
            init_params['embeddings_path'] = config.embeddings_path
            logger.info(f"Using embeddings: {config.embeddings_path}")
        
        self.sd = StableDiffusion(**init_params)
        logger.info(f"SD model loaded successfully: {config.model_name}")
    
    def is_processing(self) -> bool:
        """Check if SD is currently processing a request"""
        return self._processing_lock.locked()
    
    async def txt2img_safe(self,
                          prompt: str,
                          negative_prompt: str = "",
                          width: int = None,
                          height: int = None,
                          cfg_scale: float = None,
                          steps: int = None,
                          seed: int = -1,
                          sampler: str = None) -> Dict[str, Any]:
        """Thread-safe text-to-image generation"""
        async with self._processing_lock:
            loop = asyncio.get_event_loop()
            
            return await loop.run_in_executor(
                None,
                lambda: self.txt2img(
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    width=width,
                    height=height,
                    cfg_scale=cfg_scale,
                    steps=steps,
                    seed=seed,
                    sampler=sampler
                )
            )
    
    def txt2img(self,
                prompt: str,
                negative_prompt: str = "",
                width: int = None,
                height: int = None,
                cfg_scale: float = None,
                steps: int = None,
                seed: int = -1,
                sampler: str = None) -> Dict[str, Any]:
        """
        Generate image from text prompt
        
        Args:
            prompt: Text description of desired image
            negative_prompt: What to avoid in the image
            width: Image width (default from config)
            height: Image height (default from config)
            cfg_scale: Classifier-free guidance scale (default from config)
            steps: Number of sampling steps (default from config)
            seed: Random seed (-1 for random)
            sampler: Sampling method (default from config)
        
        Returns:
            Dict with image data and metadata
        """
        self.metrics_manager.record_request_start()
        start_time = time.time()
        
        # Use config defaults if not specified
        width = width or self.config.default_width
        height = height or self.config.default_height
        cfg_scale = cfg_scale or self.config.default_cfg_scale
        steps = steps or self.config.default_steps
        sampler = sampler or self.config.default_sampler
        
        try:
            # Generate image
            output = self.sd.txt_to_img(
                prompt=prompt,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                cfg_scale=cfg_scale,
                sample_steps=steps,
                seed=seed,
                sample_method=sampler
            )
            
            generation_time = time.time() - start_time
            
            # Convert output to base64
            image_b64 = self._image_to_base64(output)
            
            result = {
                "image": image_b64,
                "format": "png",
                "width": width,
                "height": height,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "cfg_scale": cfg_scale,
                "steps": steps,
                "seed": seed,
                "sampler": sampler,
                "generation_time": generation_time
            }
            
            return result
            
        finally:
            generation_time = time.time() - start_time
            # Track as 1 "token" per image for metrics consistency
            self.metrics_manager.record_request_end(1, generation_time)
    
    async def img2img_safe(self,
                          init_image: str,
                          prompt: str,
                          negative_prompt: str = "",
                          strength: float = 0.75,
                          cfg_scale: float = None,
                          steps: int = None,
                          seed: int = -1,
                          sampler: str = None) -> Dict[str, Any]:
        """Thread-safe image-to-image generation"""
        async with self._processing_lock:
            loop = asyncio.get_event_loop()
            
            return await loop.run_in_executor(
                None,
                lambda: self.img2img(
                    init_image=init_image,
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    strength=strength,
                    cfg_scale=cfg_scale,
                    steps=steps,
                    seed=seed,
                    sampler=sampler
                )
            )
    
    def img2img(self,
                init_image: str,
                prompt: str,
                negative_prompt: str = "",
                strength: float = 0.75,
                cfg_scale: float = None,
                steps: int = None,
                seed: int = -1,
                sampler: str = None) -> Dict[str, Any]:
        """
        Generate image from initial image and prompt
        
        Args:
            init_image: Base64 encoded initial image
            prompt: Text description of desired modifications
            negative_prompt: What to avoid in the image
            strength: How much to transform the image (0.0-1.0)
            cfg_scale: Classifier-free guidance scale
            steps: Number of sampling steps
            seed: Random seed (-1 for random)
            sampler: Sampling method
        
        Returns:
            Dict with image data and metadata
        """
        self.metrics_manager.record_request_start()
        start_time = time.time()
        
        # Use config defaults if not specified
        cfg_scale = cfg_scale or self.config.default_cfg_scale
        steps = steps or self.config.default_steps
        sampler = sampler or self.config.default_sampler
        
        try:
            # Decode base64 image
            init_img = self._base64_to_image(init_image)
            
            # Generate image
            output = self.sd.img_to_img(
                init_image=init_img,
                prompt=prompt,
                negative_prompt=negative_prompt,
                strength=strength,
                cfg_scale=cfg_scale,
                sample_steps=steps,
                seed=seed,
                sample_method=sampler
            )
            
            generation_time = time.time() - start_time
            
            # Convert output to base64
            image_b64 = self._image_to_base64(output)
            
            result = {
                "image": image_b64,
                "format": "png",
                "width": output.width,
                "height": output.height,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "strength": strength,
                "cfg_scale": cfg_scale,
                "steps": steps,
                "seed": seed,
                "sampler": sampler,
                "generation_time": generation_time
            }
            
            return result
            
        finally:
            generation_time = time.time() - start_time
            self.metrics_manager.record_request_end(1, generation_time)
    
    def _image_to_base64(self, image: Image.Image) -> str:
        """Convert PIL Image to base64 string"""
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        img_bytes = buffered.getvalue()
        return base64.b64encode(img_bytes).decode('utf-8')
    
    def _base64_to_image(self, image_b64: str) -> Image.Image:
        """Convert base64 string to PIL Image"""
        # Remove data URL prefix if present
        if ',' in image_b64:
            image_b64 = image_b64.split(',')[1]
        
        img_bytes = base64.b64decode(image_b64)
        return Image.open(io.BytesIO(img_bytes))
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get metrics about the SD model"""
        metrics = self.metrics_manager.get_comprehensive_metrics()
        metrics["model"] = self.config.model_name
        metrics["model_type"] = self.config.model_type
        metrics["model_info"] = self.config.get_model_info()
        
        return metrics
    
    def get_supported_samplers(self) -> List[str]:
        """Get list of supported sampling methods"""
        return [
            "euler_a",
            "euler",
            "heun",
            "dpm2",
            "dpm++2s_a",
            "dpm++2m",
            "dpm++2mv2",
            "lcm"
        ]
