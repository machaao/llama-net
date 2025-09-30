import os
import sys
from typing import Optional, Dict
from common.utils import load_env_var, get_logger

logger = get_logger(__name__)

class SDConfig:
    """Configuration for Stable Diffusion inference node"""
    
    def __init__(self, 
                 model_path: str = None,
                 vae_path: str = None,
                 taesd_path: str = None,
                 control_net_path: str = None,
                 lora_model_dir: str = None,
                 embeddings_path: str = None):
        """
        Initialize SD configuration
        
        Args:
            model_path: Path to the main SD model (GGUF, safetensors, or ckpt)
            vae_path: Optional path to VAE model
            taesd_path: Optional path to TAESD model for fast preview
            control_net_path: Optional path to ControlNet model
            lora_model_dir: Optional directory containing LoRA models
            embeddings_path: Optional path to embeddings directory
        """
        # Model paths
        self.model_path = model_path or load_env_var("SD_MODEL_PATH", "")
        self.vae_path = vae_path or load_env_var("SD_VAE_PATH", None)
        self.taesd_path = taesd_path or load_env_var("SD_TAESD_PATH", None)
        self.control_net_path = control_net_path or load_env_var("SD_CONTROL_NET_PATH", None)
        self.lora_model_dir = lora_model_dir or load_env_var("SD_LORA_MODEL_DIR", None)
        self.embeddings_path = embeddings_path or load_env_var("SD_EMBEDDINGS_PATH", None)
        
        # Validate model path
        if not self.model_path:
            logger.error("SD model path is required. Use SD_MODEL_PATH environment variable")
            sys.exit(1)
        
        if not os.path.exists(self.model_path):
            logger.error(f"SD model file not found: {self.model_path}")
            sys.exit(1)
        
        # Hardware configuration
        self.n_threads = int(load_env_var("SD_N_THREADS", -1))  # -1 = auto-detect
        self.wtype = load_env_var("SD_WTYPE", "default")  # Weight type: default, f32, f16, q4_0, q4_1, q5_0, q5_1, q8_0
        
        # Generation defaults
        self.default_width = int(load_env_var("SD_DEFAULT_WIDTH", 512))
        self.default_height = int(load_env_var("SD_DEFAULT_HEIGHT", 512))
        self.default_steps = int(load_env_var("SD_DEFAULT_STEPS", 20))
        self.default_cfg_scale = float(load_env_var("SD_DEFAULT_CFG_SCALE", 7.0))
        self.default_sampler = load_env_var("SD_DEFAULT_SAMPLER", "euler_a")
        
        # Performance settings
        self.vae_tiling = bool(load_env_var("SD_VAE_TILING", False))
        self.free_params_immediately = bool(load_env_var("SD_FREE_PARAMS_IMMEDIATELY", False))
        
        # Extract model name from path
        self.model_name = os.path.basename(self.model_path)
        
        # Detect model type from extension
        self.model_type = self._detect_model_type()
        
        logger.info(f"SD Configuration initialized: {self.model_name} ({self.model_type})")
    
    def _detect_model_type(self) -> str:
        """Detect model type from file extension"""
        ext = os.path.splitext(self.model_path)[1].lower()
        
        if ext == '.gguf':
            return 'gguf'
        elif ext == '.safetensors':
            return 'safetensors'
        elif ext in ['.ckpt', '.pt', '.pth']:
            return 'ckpt'
        else:
            logger.warning(f"Unknown model type for extension {ext}, assuming safetensors")
            return 'safetensors'
    
    def get_model_info(self) -> Dict:
        """Get model information for API responses"""
        return {
            "model_name": self.model_name,
            "model_type": self.model_type,
            "model_path": self.model_path,
            "has_vae": self.vae_path is not None,
            "has_taesd": self.taesd_path is not None,
            "has_controlnet": self.control_net_path is not None,
            "has_lora": self.lora_model_dir is not None,
            "default_size": f"{self.default_width}x{self.default_height}",
            "default_steps": self.default_steps,
            "default_sampler": self.default_sampler
        }
    
    def get_configuration_summary(self) -> Dict:
        """Get configuration summary for debugging"""
        return {
            "model_name": self.model_name,
            "model_type": self.model_type,
            "model_path": self.model_path,
            "vae_path": self.vae_path,
            "taesd_path": self.taesd_path,
            "control_net_path": self.control_net_path,
            "lora_model_dir": self.lora_model_dir,
            "embeddings_path": self.embeddings_path,
            "n_threads": self.n_threads,
            "wtype": self.wtype,
            "defaults": {
                "width": self.default_width,
                "height": self.default_height,
                "steps": self.default_steps,
                "cfg_scale": self.default_cfg_scale,
                "sampler": self.default_sampler
            },
            "performance": {
                "vae_tiling": self.vae_tiling,
                "free_params_immediately": self.free_params_immediately
            }
        }
    
    def __str__(self) -> str:
        return (
            f"SDConfig(model_path={self.model_path}, "
            f"model_type={self.model_type}, "
            f"default_size={self.default_width}x{self.default_height})"
        )
