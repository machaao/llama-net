import os
import re
import json
import hashlib
import requests
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from tqdm import tqdm
from common.utils import get_logger

logger = get_logger(__name__)

class HFModelDownloader:
    """Download models from Hugging Face"""
    
    # Pattern for Hugging Face URLs
    HF_URL_PATTERNS = [
        # Full Hugging Face model URLs
        r'hf\.co/([^/]+/[^/]+)',
        r'huggingface\.co/([^/]+/[^/]+)',
        # Short format: user/model
        r'^([a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+)$',
        # With tag: user/model:tag
        r'^([a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+)$',
        # With quantization: user/model:Q4_K_M
        r'^([a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+):(Q[0-9]+_[A-Z]+[A-Z0-9_]*)$',
        # With branch/tag: user/model@branch
        r'^([a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+)@([a-zA-Z0-9_.-]+)$'
    ]
    
    def __init__(self, cache_dir: Optional[str] = None):
        """
        Initialize downloader
        
        Args:
            cache_dir: Custom cache directory (default: ~/.llamanet/models)
        """
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        else:
            self.cache_dir = Path.home() / ".llamanet" / "models"
        
        # Create cache directory if it doesn't exist
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Metadata file for tracking downloaded models
        self.metadata_file = self.cache_dir / "models_metadata.json"
        self._load_metadata()
    
    def _load_metadata(self):
        """Load metadata about downloaded models"""
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, 'r') as f:
                    self.metadata = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.metadata = {}
        else:
            self.metadata = {}
    
    def _save_metadata(self):
        """Save metadata about downloaded models"""
        try:
            with open(self.metadata_file, 'w') as f:
                json.dump(self.metadata, f, indent=2)
        except IOError as e:
            logger.warning(f"Failed to save model metadata: {e}")
    
    def parse_hf_url(self, hf_url: str) -> Tuple[str, Optional[str], Optional[str]]:
        """
        Parse Hugging Face URL into components
        
        Returns:
            Tuple of (repo_id, filename, tag)
        """
        # Clean the URL
        hf_url = hf_url.strip().rstrip('/')
        
        # Remove scheme if present (http://, https://)
        if '://' in hf_url:
            hf_url = hf_url.split('://', 1)[1]
        
        # Remove domain prefixes (hf.co/, huggingface.co/)
        for prefix in ['hf.co/', 'huggingface.co/']:
            if hf_url.startswith(prefix):
                hf_url = hf_url[len(prefix):]
                break
        
        # Now hf_url should be in format: user/model or user/model:tag or user/model@branch
        
        # Check for tag/quantization separator (: or @)
        # Use rsplit to split from the RIGHT to handle dots in model names
        if ':' in hf_url:
            # Split on the LAST colon to handle model names like "LFM2.5-1.2B"
            repo_id, tag = hf_url.rsplit(':', 1)
            
            # Validate that we have a proper user/model format
            if '/' in repo_id and repo_id.count('/') == 1:
                logger.debug(f"Parsed HF URL: repo_id={repo_id}, tag={tag}")
                return repo_id, None, tag
        
        if '@' in hf_url:
            # Split on the LAST @ to handle edge cases
            repo_id, tag = hf_url.rsplit('@', 1)
            
            # Validate that we have a proper user/model format
            if '/' in repo_id and repo_id.count('/') == 1:
                logger.debug(f"Parsed HF URL: repo_id={repo_id}, tag={tag}")
                return repo_id, None, tag
        
        # No tag/branch specified - check if it's a valid repo_id format
        if '/' in hf_url and hf_url.count('/') == 1:
            logger.debug(f"Parsed HF URL: repo_id={hf_url}, no tag")
            return hf_url, None, None
        
        # If we get here, the format is invalid
        raise ValueError(f"Invalid Hugging Face URL format: {hf_url}. Expected format: user/model or user/model:tag")
    
    def get_model_info(self, repo_id: str, tag: Optional[str] = None) -> Dict:
        """
        Get model information from Hugging Face API
        
        Args:
            repo_id: Repository ID (e.g., "meta-llama/Llama-2-7b-chat-hf")
            tag: Optional tag/branch
            
        Returns:
            Dictionary with model info
        """
        try:
            # Construct API URL - repo_id should NOT contain the tag
            api_url = f"https://huggingface.co/api/models/{repo_id}"
            
            # Add revision parameter if tag is specified
            if tag:
                api_url += f"?revision={tag}"
            
            logger.debug(f"Fetching model info from: {api_url}")
            
            response = requests.get(api_url, timeout=30)
            response.raise_for_status()
            
            model_info = response.json()
            
            # Find GGUF files in the model
            gguf_files = []
            if 'siblings' in model_info:
                for sibling in model_info['siblings']:
                    filename = sibling.get('rfilename', '')
                    if filename.lower().endswith('.gguf'):
                        gguf_files.append(filename)
            
            logger.debug(f"Found {len(gguf_files)} GGUF files in {repo_id}")
            
            return {
                'repo_id': repo_id,
                'tag': tag,
                'model_name': model_info.get('modelId', repo_id.split('/')[-1]),
                'downloads': model_info.get('downloads', 0),
                'likes': model_info.get('likes', 0),
                'gguf_files': gguf_files,
                'sha': model_info.get('sha', ''),
                'last_modified': model_info.get('lastModified', ''),
            }
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                logger.error(f"Unauthorized access to {repo_id}. This model may be gated.")
                logger.error("Please authenticate with Hugging Face:")
                logger.error("  1. Get a token from https://huggingface.co/settings/tokens")
                logger.error("  2. Run: huggingface-cli login")
                logger.error("  3. Accept the model's terms on Hugging Face if required")
            elif e.response.status_code == 404:
                logger.error(f"Model not found: {repo_id}")
            raise
        except requests.RequestException as e:
            logger.error(f"Failed to fetch model info for {repo_id}: {e}")
            raise
    
    def find_gguf_file(self, repo_id: str, tag: Optional[str] = None, 
                      quantization: Optional[str] = None) -> str:
        """
        Find appropriate GGUF file in repository
        
        Args:
            repo_id: Repository ID
            tag: Optional tag/branch
            quantization: Desired quantization (e.g., Q4_K_M)
            
        Returns:
            Filename of the GGUF file
        """
        model_info = self.get_model_info(repo_id, tag)
        gguf_files = model_info['gguf_files']
        
        if not gguf_files:
            raise ValueError(f"No GGUF files found in repository {repo_id}")
        
        # If quantization specified, try to find exact match
        if quantization:
            for filename in gguf_files:
                if quantization.upper() in filename.upper():
                    return filename
        
        # Prefer files with common quantization patterns
        preferred_quantizations = ['Q4_K_M', 'Q4_K_S', 'Q5_K_M', 'Q5_K_S', 'Q8_0', 'F16']
        
        for pref_quant in preferred_quantizations:
            for filename in gguf_files:
                if pref_quant in filename.upper():
                    return filename
        
        # If no preferred quantization found, use the first GGUF file
        return gguf_files[0]
    
    def get_local_model_path(self, repo_id: str, filename: str, 
                            tag: Optional[str] = None, 
                            quantization: Optional[str] = None) -> Path:
        """
        Get local path for a model
        
        Args:
            repo_id: Repository ID
            filename: GGUF filename
            tag: Optional tag/branch
            quantization: Optional quantization
            
        Returns:
            Path to local model file
        """
        # Create unique model directory name
        model_name = repo_id.replace('/', '_')
        if tag:
            model_name += f"@{tag}"
        if quantization:
            model_name += f":{quantization}"
        
        model_dir = self.cache_dir / model_name
        model_dir.mkdir(parents=True, exist_ok=True)
        
        return model_dir / filename
    
    def download_model(self, hf_url: str, force_download: bool = False) -> str:
        """
        Download a model from Hugging Face
        
        Args:
            hf_url: Hugging Face URL (e.g., "hf.co/meta-llama/Llama-2-7b-chat-hf:Q4_K_M")
            force_download: Force re-download even if cached
            
        Returns:
            Path to downloaded model file
        """
        try:
            # Parse the URL
            repo_id, filename, tag = self.parse_hf_url(hf_url)
            
            # Determine quantization from tag if it looks like one
            quantization = None
            if tag and re.match(r'^Q[0-9]+_[A-Z0-9_]+$', tag):
                quantization = tag
                tag = None
            
            # Find appropriate GGUF file
            if not filename:
                filename = self.find_gguf_file(repo_id, tag, quantization)
            
            # Get local path
            local_path = self.get_local_model_path(repo_id, filename, tag, quantization)
            
            # Check if already downloaded
            if local_path.exists() and not force_download:
                # Verify file hash if we have it
                model_key = f"{repo_id}/{filename}"
                if model_key in self.metadata:
                    expected_hash = self.metadata[model_key].get('sha256')
                    if expected_hash and self._verify_file_hash(local_path, expected_hash):
                        logger.info(f"Model already downloaded and verified: {local_path}")
                        return str(local_path)
                    else:
                        logger.warning(f"Model hash mismatch, re-downloading: {local_path}")
                else:
                    logger.info(f"Model already downloaded: {local_path}")
                    return str(local_path)
            
            # Download the model
            logger.info(f"Downloading model {repo_id}/{filename}...")
            
            # Construct download URL
            download_url = f"https://huggingface.co/{repo_id}/resolve/{tag or 'main'}/{filename}"
            
            # Download with progress
            local_path = self._download_with_progress(download_url, local_path)
            
            # Calculate and store hash
            file_hash = self._calculate_file_hash(local_path)
            model_key = f"{repo_id}/{filename}"
            
            self.metadata[model_key] = {
                'repo_id': repo_id,
                'filename': filename,
                'tag': tag,
                'quantization': quantization,
                'download_url': download_url,
                'local_path': str(local_path),
                'sha256': file_hash,
                'download_time': str(Path(local_path).stat().st_mtime),
            }
            
            self._save_metadata()
            
            logger.info(f"✅ Model downloaded successfully: {local_path}")
            return str(local_path)
            
        except Exception as e:
            logger.error(f"Failed to download model: {e}")
            raise
    
    def _download_with_progress(self, url: str, local_path: Path) -> Path:
        """Download file with progress bar"""
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        # Get file size
        total_size = int(response.headers.get('content-length', 0))
        
        # Download with progress bar
        with open(local_path, 'wb') as f:
            with tqdm(
                desc=f"Downloading {local_path.name}",
                total=total_size,
                unit='B',
                unit_scale=True,
                unit_divisor=1024,
            ) as pbar:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        pbar.update(len(chunk))
        
        return local_path
    
    def _calculate_file_hash(self, file_path: Path) -> str:
        """Calculate SHA256 hash of a file"""
        sha256_hash = hashlib.sha256()
        
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256_hash.update(chunk)
        
        return sha256_hash.hexdigest()
    
    def _verify_file_hash(self, file_path: Path, expected_hash: str) -> bool:
        """Verify file hash"""
        if not file_path.exists():
            return False
        
        actual_hash = self._calculate_file_hash(file_path)
        return actual_hash == expected_hash
    
    def list_downloaded_models(self) -> List[Dict]:
        """List all downloaded models"""
        models = []
        
        for model_key, model_info in self.metadata.items():
            local_path = Path(model_info.get('local_path', ''))
            
            models.append({
                'repo_id': model_info.get('repo_id'),
                'filename': model_info.get('filename'),
                'tag': model_info.get('tag'),
                'quantization': model_info.get('quantization'),
                'local_path': str(local_path),
                'exists': local_path.exists(),
                'size_mb': local_path.stat().st_size / (1024 * 1024) if local_path.exists() else 0,
                'download_time': model_info.get('download_time'),
            })
        
        return models
    
    def remove_model(self, repo_id: str, filename: Optional[str] = None) -> bool:
        """Remove a downloaded model"""
        model_key = f"{repo_id}/{filename}" if filename else repo_id
        
        # Find matching models
        models_to_remove = []
        for key in self.metadata.keys():
            if key.startswith(repo_id):
                if filename is None or filename in key:
                    models_to_remove.append(key)
        
        removed = False
        for key in models_to_remove:
            model_info = self.metadata[key]
            local_path = Path(model_info.get('local_path', ''))
            
            if local_path.exists():
                local_path.unlink()
                removed = True
            
            # Also try to remove parent directory if empty
            parent_dir = local_path.parent
            if parent_dir.exists() and not any(parent_dir.iterdir()):
                parent_dir.rmdir()
            
            del self.metadata[key]
        
        if removed:
            self._save_metadata()
            logger.info(f"Removed model {repo_id}")
        
        return removed
