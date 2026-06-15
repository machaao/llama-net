import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from common.utils import get_logger
from inference_node.hf_downloader import HFModelDownloader

logger = get_logger(__name__)

class ModelManager:
    """Manage local models and Hugging Face downloads"""
    
    # Regex pattern for HF URLs
    HF_PATTERN = re.compile(
        r'^(?:https?://)?(?:www\.)?'
        r'(?:hf\.co|huggingface\.co)/'
        r'([a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+)'
        r'(?:[:@]([a-zA-Z0-9_.-]+))?$'
    )
    
    # Simple pattern for user/model format
    SIMPLE_PATTERN = re.compile(r'^([a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+)(?:[:@]([a-zA-Z0-9_.-]+))?$')
    
    def __init__(self, cache_dir: Optional[str] = None):
        """
        Initialize model manager
        
        Args:
            cache_dir: Custom cache directory
        """
        self.downloader = HFModelDownloader(cache_dir)
        self.cache_dir = self.downloader.cache_dir
    
    def is_hf_url(self, model_path: str) -> bool:
        """Check if a string is a Hugging Face URL"""
        if not model_path:
            return False
        
        # Check for HF URL patterns
        if self.HF_PATTERN.match(model_path):
            return True
        
        # Check for simple user/model format
        if self.SIMPLE_PATTERN.match(model_path) and '/' in model_path:
            # Additional validation: check if it looks like a HF repo
            parts = model_path.split('/')
            if len(parts) == 2 and not parts[0].startswith('.'):
                return True
        
        return False
    
    def parse_model_identifier(self, model_identifier: str) -> Tuple[str, Optional[str], Optional[str]]:
        """
        Parse model identifier
        
        Returns:
            Tuple of (repo_id, filename, tag/quantization)
        """
        # Try HF URL pattern first
        match = self.HF_PATTERN.match(model_identifier)
        if match:
            repo_id = match.group(1)
            tag = match.group(2)
            return repo_id, None, tag
        
        # Try simple pattern
        match = self.SIMPLE_PATTERN.match(model_identifier)
        if match:
            repo_id = match.group(1)
            tag = match.group(2)
            return repo_id, None, tag
        
        # If it's a local path, return as is
        if os.path.exists(model_identifier):
            return model_identifier, None, None
        
        raise ValueError(f"Invalid model identifier: {model_identifier}")
    
    def resolve_model_path(self, model_identifier: str, force_download: bool = False) -> str:
        """
        Resolve model identifier to local path
        
        Args:
            model_identifier: HF URL, user/model format, or local path
            force_download: Force re-download
            
        Returns:
            Path to model file
        """
        # If it's already a local file path and exists, return it
        if os.path.exists(model_identifier):
            logger.info(f"Using local model: {model_identifier}")
            return model_identifier
        
        # If it looks like a HF URL/repo
        if self.is_hf_url(model_identifier):
            logger.info(f"Downloading model from Hugging Face: {model_identifier}")
            return self.downloader.download_model(model_identifier, force_download)
        
        # Otherwise, treat as local path (even if it doesn't exist yet)
        return model_identifier
    
    def list_models(self) -> Dict:
        """List all available models"""
        # List downloaded models
        downloaded = self.downloader.list_downloaded_models()
        
        # List any models in the cache directory directly
        cache_models = []
        for item in self.cache_dir.iterdir():
            if item.is_dir():
                # Look for GGUF files in the directory
                gguf_files = list(item.glob("*.gguf"))
                if gguf_files:
                    cache_models.append({
                        'name': item.name,
                        'path': str(gguf_files[0]),
                        'size_mb': gguf_files[0].stat().st_size / (1024 * 1024),
                        'source': 'cache',
                    })
        
        return {
            'downloaded': downloaded,
            'cache': cache_models,
            'cache_dir': str(self.cache_dir),
        }
    
    def remove_model(self, model_identifier: str) -> bool:
        """Remove a model"""
        if self.is_hf_url(model_identifier):
            repo_id, _, tag = self.parse_model_identifier(model_identifier)
            return self.downloader.remove_model(repo_id)
        else:
            # Try to remove from cache
            model_path = Path(model_identifier)
            if model_path.exists():
                model_path.unlink()
                return True
            return False
