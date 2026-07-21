"""Model downloader for HuggingFace GGUF models"""

import os
import sys
import time
import requests
from typing import Optional, List, Dict
from common.utils import get_logger
from common.hf_utils import get_model_dir, get_model_path, format_size

logger = get_logger(__name__)

HF_API_BASE = "https://huggingface.co/api/models"
HF_FILE_BASE = "https://huggingface.co"

def list_gguf_files(repo_id: str) -> List[Dict]:
    """List all GGUF files in a HuggingFace repository
    
    Args:
        repo_id: HuggingFace repository ID (org/model)
        
    Returns:
        List of dicts with file info (filename, size, download_url)
    """
    api_url = f"{HF_API_BASE}/{repo_id}/tree/main"
    
    logger.debug(f"Querying HuggingFace API: {api_url}")
    
    try:
        response = requests.get(api_url, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Failed to query HuggingFace API: {e}")
    
    files = response.json()
    gguf_files = []
    
    for file_info in files:
        if file_info.get("type") != "file":
            continue
        
        filename = file_info.get("path", "")
        if not filename.endswith(".gguf"):
            continue
        
        # Extract quantization from filename
        quantization = extract_quantization(filename)
        
        gguf_files.append({
            "filename": filename,
            "size": file_info.get("size", 0),
            "quantization": quantization,
            "download_url": f"{HF_FILE_BASE}/{repo_id}/resolve/main/{filename}"
        })
    
    logger.debug(f"Found {len(gguf_files)} GGUF files in {repo_id}")
    return gguf_files

def extract_quantization(filename: str) -> Optional[str]:
    """Extract quantization level from GGUF filename
    
    Examples:
        model-Q4_K_M.gguf -> Q4_K_M
        LFM2-1.2B-Q8_0.gguf -> Q8_0
        some_model_f16.gguf -> F16
    """
    # Common patterns for quantization in filenames
    quant_patterns = [
        r'[.-]?(Q\d+[A-Z]*(?:_[A-Z]+)*)',  # Q4_K_M, Q8_0, Q5_K_S
        r'[.-]?(IQ\d+[A-Z]*(?:_[A-Z]+)*)',  # IQ4_XS, IQ2_XXS
        r'[.-]?(F(?:16|32))',                 # F16, F32
        r'[.-]?(BF16)',                        # BF16
    ]
    
    import re
    filename_upper = filename.upper()
    
    for pattern in quant_patterns:
        match = re.search(pattern, filename_upper)
        if match:
            return match.group(1)
    
    return None

def match_quantization(gguf_files: List[Dict], target_quant: str) -> Optional[Dict]:
    """Find the best matching GGUF file for a target quantization
    
    Args:
        gguf_files: List of GGUF file info dicts
        target_quant: Target quantization level (e.g., Q4_K_M)
        
    Returns:
        Best matching file info dict, or None if no match
    """
    target_quant = target_quant.upper().replace("-", "_")
    
    # Exact match
    for file_info in gguf_files:
        if file_info["quantization"] == target_quant:
            logger.info(f"Exact match found: {file_info['filename']}")
            return file_info
    
    # Try partial matches
    # For example, if target is Q4_K_M, try Q4_K_M, Q4_K, Q4
    target_parts = target_quant.split("_")
    
    for length in range(len(target_parts), 0, -1):
        partial = "_".join(target_parts[:length])
        for file_info in gguf_files:
            if file_info["quantization"] and file_info["quantization"].startswith(partial):
                logger.info(f"Partial match found: {file_info['filename']} (target: {target_quant})")
                return file_info
    
    # No match found
    return None

def select_best_quantization(gguf_files: List[Dict]) -> Optional[Dict]:
    """Auto-select the best quantization if user didn't specify one
    
    Preference order:
    1. Q4_K_M (best balance of size/quality)
    2. Q5_K_M (slightly larger, better quality)
    3. Q4_K_S (smaller)
    4. Q8_0 (larger, high quality)
    5. Any Q4
    6. Any file
    """
    preferred_order = ["Q4_K_M", "Q5_K_M", "Q4_K_S", "Q5_K_S", "Q8_0", "Q6_K"]
    
    for pref in preferred_order:
        for file_info in gguf_files:
            if file_info["quantization"] == pref:
                logger.info(f"Auto-selected quantization: {pref}")
                return file_info
    
    # Fall back to any Q4 or Q5
    for file_info in gguf_files:
        if file_info["quantization"] and file_info["quantization"].startswith(("Q4", "Q5")):
            logger.info(f"Fallback quantization: {file_info['quantization']}")
            return file_info
    
    # Last resort: first file
    if gguf_files:
        logger.info(f"No preferred quantization found, using first file: {gguf_files[0]['filename']}")
        return gguf_files[0]
    
    return None

def download_with_progress(url: str, dest_path: str, file_size: int = 0, quiet: bool = False) -> str:
    """Download a file with progress tracking
    
    Args:
        url: URL to download from
        dest_path: Destination file path
        file_size: Expected file size in bytes (for progress bar)
        quiet: If True, suppress progress output
        
    Returns:
        Path to downloaded file
    """
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    
    # Download to temp file first
    temp_path = dest_path + ".downloading"
    
    try:
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()
        
        # Get content length if not provided
        if file_size == 0:
            content_length = response.headers.get("Content-Length")
            if content_length:
                file_size = int(content_length)
        
        downloaded = 0
        start_time = time.time()
        last_update = 0
        
        with open(temp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192 * 4):  # 32KB chunks
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    # Update progress (not too often)
                    current_time = time.time()
                    if current_time - last_update >= 0.1 or downloaded == file_size:
                        last_update = current_time
                        if not quiet:
                            _print_progress(downloaded, file_size, start_time)
        
        # Final newline after progress
        if not quiet and file_size > 0:
            print()
        
        # Move temp file to final destination
        if os.path.exists(dest_path):
            os.remove(dest_path)
        os.rename(temp_path, dest_path)
        
        logger.info(f"Downloaded: {dest_path} ({format_size(downloaded)})")
        return dest_path
        
    except Exception as e:
        # Clean up temp file on error
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise

def _print_progress(downloaded: int, total: int, start_time: float):
    """Print download progress bar"""
    if total > 0:
        percent = (downloaded / total) * 100
        bar_length = 40
        filled = int(bar_length * downloaded / total)
        bar = "█" * filled + "░" * (bar_length - filled)
        
        elapsed = time.time() - start_time
        if elapsed > 0:
            speed = downloaded / elapsed
            speed_str = f"{format_size(int(speed))}/s"
        else:
            speed_str = "calculating..."
        
        sys.stdout.write(f"\r⬇️  [{bar}] {percent:.1f}% | {format_size(downloaded)}/{format_size(total)} | {speed_str}")
        sys.stdout.flush()
    else:
        # Unknown total size
        elapsed = time.time() - start_time
        if elapsed > 0:
            speed = downloaded / elapsed
            speed_str = f"{format_size(int(speed))}/s"
        else:
            speed_str = "calculating..."
        
        sys.stdout.write(f"\r⬇️  Downloaded: {format_size(downloaded)} | {speed_str}")
        sys.stdout.flush()

def download_model(repo_id: str, quantization: Optional[str] = None, quiet: bool = False) -> str:
    """Download a GGUF model from HuggingFace
    
    Args:
        repo_id: HuggingFace repository ID (org/model)
        quantization: Target quantization level (e.g., Q4_K_M). If None, auto-selects.
        quiet: If True, suppress progress output
        
    Returns:
        Path to downloaded model file
        
    Raises:
        ValueError: If no GGUF files found or quantization not available
        ConnectionError: If API or download fails
    """
    if not quiet:
        print(f"\n📡 Querying HuggingFace for {repo_id}...")
    
    # List available GGUF files
    gguf_files = list_gguf_files(repo_id)
    
    if not gguf_files:
        raise ValueError(f"No GGUF files found in {repo_id}")
    
    if not quiet:
        print(f"   Found {len(gguf_files)} GGUF file(s)")
    
    # Match requested quantization
    if quantization:
        matched_file = match_quantization(gguf_files, quantization)
        if not matched_file:
            available = [f["quantization"] for f in gguf_files if f["quantization"]]
            raise ValueError(
                f"Quantization '{quantization}' not found.\n"
                f"Available: {', '.join(available)}"
            )
    else:
        matched_file = select_best_quantization(gguf_files)
        if not matched_file:
            raise ValueError("Could not select a suitable model file")
    
    # Get target path
    target_path = get_model_path(repo_id, matched_file["quantization"])
    
    if not quiet:
        print(f"   Selected: {matched_file['filename']} ({format_size(matched_file['size'])})")
        print(f"   Saving to: {target_path}\n")
    
    # Download
    download_with_progress(
        matched_file["download_url"],
        target_path,
        file_size=matched_file["size"],
        quiet=quiet
    )
    
    return target_path
