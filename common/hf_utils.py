"""HuggingFace utilities for parsing model URLs and resolving model paths"""

import os
import re
import sys
from typing import Optional, Tuple
from common.utils import get_logger

logger = get_logger(__name__)

# Default model storage directory
DEFAULT_MODEL_DIR = os.path.expanduser("~/.llamanet/models")

# Supported quantization levels (ordered by preference)
QUANTIZATION_LEVELS = [
    "Q2_K", "Q3_K_S", "Q3_K_M", "Q3_K_L",
    "Q4_0", "Q4_K_S", "Q4_K_M", "Q5_0", "Q5_K_S", "Q5_K_M",
    "Q6_K", "Q8_0",
    "IQ2_XXS", "IQ2_XS", "IQ3_XXS", "IQ4_XS", "IQ4_NL",
    "F16", "F32", "BF16"
]

def is_huggingface_url(path: str) -> bool:
    """Check if a path is a HuggingFace model URL or shorthand"""
    if not path:
        return False
    
    # Full URLs
    if path.startswith(("hf.co/", "huggingface.co/", "https://hf.co/", "https://huggingface.co/")):
        return True
    
    # Shorthand format: Repo/Model:Quantization
    # Must contain at least one slash and optionally a colon with quantization
    if "/" in path and not path.startswith(("/", "./", "../")):
        # Check if it looks like a HF repo path (org/model format)
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] and parts[1]:
            # Avoid matching local paths like "models/something"
            if not os.path.exists(path) and not path.startswith(("models/", "model/")):
                return True
    
    return False

def parse_hf_url(url: str) -> Tuple[str, Optional[str]]:
    """Parse a HuggingFace URL into repo_id and quantization
    
    Args:
        url: HuggingFace URL or shorthand
        
    Returns:
        Tuple of (repo_id, quantization)
        
    Examples:
        hf.co/LiquidAI/LFM2.1.2B-JP-202606-GGUF:Q4_K_M
            -> ("LiquidAI/LFM2-1.2B-JP-202606-GGUF", "Q4_K_M")
        huggingface.co/meta-llama/Llama-3-8B-Instruct-GGUF
            -> ("meta-llama/Llama-3-8B-Instruct-GGUF", None)
        meta-llama/Llama-3-8B-Instruct-GGUF:Q5_K_M
            -> ("meta-llama/Llama-3-8B-Instruct-GGUF", "Q5_K_M")
    """
    # Remove protocol and domain
    path = url
    for prefix in ["https://hf.co/", "http://hf.co/", "hf.co/",
                    "https://huggingface.co/", "http://huggingface.co/", "huggingface.co/"]:
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    
    # Remove trailing slashes
    path = path.rstrip("/")
    
    # Split on colon for quantization
    quantization = None
    if ":" in path:
        parts = path.rsplit(":", 1)
        if len(parts) == 2:
            path, quant_tag = parts
            # Normalize quantization tag
            quantization = quant_tag.upper().replace("-", "_")
    
    # Validate repo_id format (should be org/model)
    if "/" not in path:
        raise ValueError(f"Invalid HuggingFace model path: '{path}'. Expected format: org/model")
    
    parts = path.split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid HuggingFace model path: '{path}'. Expected format: org/model")
    
    org, model = parts
    if not org or not model:
        raise ValueError(f"Invalid HuggingFace model path: '{path}'. Both org and model must be non-empty")
    
    repo_id = f"{org}/{model}"
    
    logger.debug(f"Parsed HF URL: repo_id={repo_id}, quantization={quantization}")
    return repo_id, quantization

def get_model_dir(repo_id: str, quantization: Optional[str] = None) -> str:
    """Get the local directory path for a model
    
    Args:
        repo_id: HuggingFace repository ID (org/model)
        quantization: Quantization level (e.g., Q4_K_M)
        
    Returns:
        Path to model directory
    """
    base_dir = os.environ.get("LLAMANET_MODEL_DIR", DEFAULT_MODEL_DIR)
    model_dir = os.path.join(base_dir, repo_id)
    return model_dir

def get_model_path(repo_id: str, quantization: Optional[str] = None) -> str:
    """Get the expected local path for a model file
    
    Args:
        repo_id: HuggingFace repository ID (org/model)
        quantization: Quantization level (e.g., Q4_K_M)
        
    Returns:
        Expected path to model GGUF file
    """
    model_dir = get_model_dir(repo_id, quantization)
    
    if quantization:
        # Use quantization as filename
        filename = f"{quantization}.gguf"
    else:
        # Use repo name as filename
        model_name = repo_id.split("/")[-1]
        filename = f"{model_name}.gguf"
    
    return os.path.join(model_dir, filename)

def resolve_hf_model(url: str, quiet: bool = False) -> str:
    """Resolve a HuggingFace URL to a local model path, downloading if necessary
    
    Args:
        url: HuggingFace URL or shorthand
        quiet: If True, suppress progress output
        
    Returns:
        Path to local model file
        
    Raises:
        ValueError: If URL is invalid
        FileNotFoundError: If model cannot be found or downloaded
    """
    from common.downloader import download_model
    
    # Parse the URL
    repo_id, quantization = parse_hf_url(url)
    
    # Check if already downloaded
    local_path = get_model_path(repo_id, quantization)
    if os.path.exists(local_path):
        file_size = os.path.getsize(local_path)
        if file_size > 1024 * 1024:  # Sanity check: file should be > 1MB
            logger.info(f"Model already exists: {local_path} ({file_size / (1024*1024):.1f} MB)")
            return local_path
        else:
            logger.warning(f"Existing file is too small ({file_size} bytes), re-downloading")
    
    # Download the model
    downloaded_path = download_model(repo_id, quantization, quiet=quiet)
    
    return downloaded_path

def format_size(size_bytes: int) -> str:
    """Format bytes as human readable string"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"
