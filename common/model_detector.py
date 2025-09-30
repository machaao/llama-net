import os
from typing import Dict, Any, Optional, Tuple
from pathlib import Path
import struct

from common.utils import get_logger

logger = get_logger(__name__)

class ModelType:
    """Model type constants"""
    LLM = "llm"
    STABLE_DIFFUSION = "sd"
    UNKNOWN = "unknown"

class ModelFormat:
    """Model format constants"""
    GGUF = "gguf"
    SAFETENSORS = "safetensors"
    CKPT = "ckpt"
    UNKNOWN = "unknown"

class ModelDetector:
    """Detect model type and format from file characteristics"""
    
    # GGUF magic number
    GGUF_MAGIC = 0x46554747  # "GGUF" in little-endian
    
    # Safetensors magic number (first 8 bytes)
    SAFETENSORS_MAGIC = b'\x00\x00\x00\x00\x00\x00\x00\x08'
    
    def __init__(self):
        self.logger = get_logger(__name__)
    
    def detect_model(self, model_path: str) -> Dict[str, Any]:
        """
        Detect model type and format from file
        
        Returns:
            Dict with:
                - model_type: "llm" or "sd"
                - model_format: "gguf", "safetensors", or "ckpt"
                - metadata: Additional model information
                - confidence: Detection confidence (0.0-1.0)
        """
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"Model file not found: {model_path}")
        
        file_ext = Path(model_path).suffix.lower()
        
        # Detect format first
        if file_ext == '.gguf':
            return self._detect_gguf_model(model_path)
        elif file_ext in ['.safetensors', '.sft']:
            return self._detect_safetensors_model(model_path)
        elif file_ext in ['.ckpt', '.pt', '.pth']:
            return self._detect_ckpt_model(model_path)
        else:
            self.logger.warning(f"Unknown model format: {file_ext}")
            return {
                "model_type": ModelType.UNKNOWN,
                "model_format": ModelFormat.UNKNOWN,
                "metadata": {},
                "confidence": 0.0
            }
    
    def _detect_gguf_model(self, model_path: str) -> Dict[str, Any]:
        """Detect GGUF model type (LLM vs SD)"""
        try:
            with open(model_path, 'rb') as f:
                # Read magic number
                magic = struct.unpack('<I', f.read(4))[0]
                
                if magic != self.GGUF_MAGIC:
                    self.logger.warning(f"Invalid GGUF magic number: {hex(magic)}")
                    return {
                        "model_type": ModelType.UNKNOWN,
                        "model_format": ModelFormat.GGUF,
                        "metadata": {},
                        "confidence": 0.0
                    }
                
                # Read version
                version = struct.unpack('<I', f.read(4))[0]
                
                # Read tensor count and metadata count
                tensor_count = struct.unpack('<Q', f.read(8))[0]
                metadata_count = struct.unpack('<Q', f.read(8))[0]
                
                # Read metadata to determine model type
                metadata = self._read_gguf_metadata(f, metadata_count)
                
                # Detect model type from metadata
                model_type, confidence = self._classify_gguf_model(metadata, tensor_count)
                
                return {
                    "model_type": model_type,
                    "model_format": ModelFormat.GGUF,
                    "metadata": {
                        "version": version,
                        "tensor_count": tensor_count,
                        "metadata_count": metadata_count,
                        **metadata
                    },
                    "confidence": confidence
                }
                
        except Exception as e:
            self.logger.error(f"Error detecting GGUF model: {e}")
            return {
                "model_type": ModelType.UNKNOWN,
                "model_format": ModelFormat.GGUF,
                "metadata": {"error": str(e)},
                "confidence": 0.0
            }
    
    def _read_gguf_metadata(self, f, metadata_count: int) -> Dict[str, Any]:
        """Read GGUF metadata key-value pairs"""
        metadata = {}
        
        try:
            for _ in range(min(metadata_count, 100)):  # Limit to first 100 entries
                # Read key length and key
                key_len = struct.unpack('<Q', f.read(8))[0]
                if key_len > 1024:  # Sanity check
                    break
                key = f.read(key_len).decode('utf-8', errors='ignore')
                
                # Read value type
                value_type = struct.unpack('<I', f.read(4))[0]
                
                # Read value based on type (simplified)
                if value_type == 8:  # String
                    value_len = struct.unpack('<Q', f.read(8))[0]
                    if value_len > 1024:
                        value = f"<string:{value_len}>"
                    else:
                        value = f.read(value_len).decode('utf-8', errors='ignore')
                elif value_type in [4, 5]:  # Int32, Int64
                    value = struct.unpack('<Q', f.read(8))[0]
                elif value_type in [6, 7]:  # Float32, Float64
                    value = struct.unpack('<d', f.read(8))[0]
                else:
                    # Skip unknown types
                    continue
                
                metadata[key] = value
                
        except Exception as e:
            self.logger.debug(f"Error reading GGUF metadata: {e}")
        
        return metadata
    
    def _classify_gguf_model(self, metadata: Dict[str, Any], tensor_count: int) -> Tuple[str, float]:
        """Classify GGUF model as LLM or SD based on metadata"""
        
        # Check for explicit model type indicators
        model_name = str(metadata.get('general.name', '')).lower()
        architecture = str(metadata.get('general.architecture', '')).lower()
        
        # SD indicators
        sd_indicators = [
            'stable-diffusion', 'sd-', 'sdxl', 'sd3', 'flux', 'diffusion',
            'unet', 'vae', 'clip', 'text_encoder'
        ]
        
        # LLM indicators
        llm_indicators = [
            'llama', 'mistral', 'qwen', 'gemma', 'phi', 'gpt',
            'transformer', 'decoder', 'attention', 'embedding'
        ]
        
        # Check model name and architecture
        sd_score = sum(1 for indicator in sd_indicators if indicator in model_name or indicator in architecture)
        llm_score = sum(1 for indicator in llm_indicators if indicator in model_name or indicator in architecture)
        
        # Check for specific metadata keys
        if 'sd.version' in metadata or 'diffusion.steps' in metadata:
            sd_score += 3
        
        if 'llama.context_length' in metadata or 'tokenizer.ggml.model' in metadata:
            llm_score += 3
        
        # Tensor count heuristic (SD models typically have fewer tensors)
        if tensor_count < 500:
            sd_score += 1
        else:
            llm_score += 1
        
        # Determine model type
        if sd_score > llm_score:
            confidence = min(0.9, 0.5 + (sd_score - llm_score) * 0.1)
            return ModelType.STABLE_DIFFUSION, confidence
        elif llm_score > sd_score:
            confidence = min(0.9, 0.5 + (llm_score - sd_score) * 0.1)
            return ModelType.LLM, confidence
        else:
            # Default to LLM for GGUF (most common)
            return ModelType.LLM, 0.5
    
    def _detect_safetensors_model(self, model_path: str) -> Dict[str, Any]:
        """Detect safetensors model type"""
        try:
            with open(model_path, 'rb') as f:
                # Read header size (first 8 bytes)
                header_size = struct.unpack('<Q', f.read(8))[0]
                
                if header_size > 100 * 1024 * 1024:  # Sanity check: 100MB max
                    raise ValueError(f"Invalid header size: {header_size}")
                
                # Read header JSON
                header_bytes = f.read(header_size)
                
                # Try to parse as JSON
                import json
                try:
                    header = json.loads(header_bytes.decode('utf-8'))
                except:
                    header = {}
                
                # Analyze tensor shapes and names
                model_type, confidence = self._classify_safetensors_model(header)
                
                return {
                    "model_type": model_type,
                    "model_format": ModelFormat.SAFETENSORS,
                    "metadata": {
                        "header_size": header_size,
                        "tensor_count": len(header) - 1 if '__metadata__' in header else len(header),
                        "has_metadata": '__metadata__' in header
                    },
                    "confidence": confidence
                }
                
        except Exception as e:
            self.logger.error(f"Error detecting safetensors model: {e}")
            # Safetensors are typically SD models
            return {
                "model_type": ModelType.STABLE_DIFFUSION,
                "model_format": ModelFormat.SAFETENSORS,
                "metadata": {"error": str(e)},
                "confidence": 0.6
            }
    
    def _classify_safetensors_model(self, header: Dict[str, Any]) -> Tuple[str, float]:
        """Classify safetensors model based on tensor names and shapes"""
        
        # Get tensor names
        tensor_names = [k for k in header.keys() if k != '__metadata__']
        
        # SD-specific tensor patterns
        sd_patterns = [
            'model.diffusion_model', 'first_stage_model', 'cond_stage_model',
            'unet', 'vae', 'text_encoder', 'clip'
        ]
        
        # LLM-specific tensor patterns
        llm_patterns = [
            'transformer', 'decoder', 'encoder', 'attention', 'mlp',
            'embed_tokens', 'lm_head'
        ]
        
        sd_score = sum(1 for pattern in sd_patterns 
                      for name in tensor_names if pattern in name.lower())
        llm_score = sum(1 for pattern in llm_patterns 
                       for name in tensor_names if pattern in name.lower())
        
        # Safetensors are predominantly SD models
        if sd_score > 0 or llm_score == 0:
            confidence = min(0.9, 0.7 + sd_score * 0.05)
            return ModelType.STABLE_DIFFUSION, confidence
        else:
            confidence = min(0.8, 0.6 + llm_score * 0.05)
            return ModelType.LLM, confidence
    
    def _detect_ckpt_model(self, model_path: str) -> Dict[str, Any]:
        """Detect CKPT/PyTorch model type"""
        try:
            import torch
            
            # Load checkpoint (map to CPU to avoid GPU memory issues)
            checkpoint = torch.load(model_path, map_location='cpu')
            
            # Analyze checkpoint structure
            if isinstance(checkpoint, dict):
                keys = list(checkpoint.keys())
                
                # Check for SD-specific keys
                sd_keys = ['model', 'state_dict', 'model_state_dict']
                has_sd_structure = any(k in keys for k in sd_keys)
                
                if has_sd_structure:
                    # Analyze nested structure
                    state_dict = checkpoint.get('state_dict') or checkpoint.get('model') or checkpoint
                    model_type, confidence = self._classify_state_dict(state_dict)
                else:
                    model_type = ModelType.UNKNOWN
                    confidence = 0.3
            else:
                model_type = ModelType.UNKNOWN
                confidence = 0.2
            
            return {
                "model_type": model_type,
                "model_format": ModelFormat.CKPT,
                "metadata": {
                    "checkpoint_keys": list(checkpoint.keys()) if isinstance(checkpoint, dict) else []
                },
                "confidence": confidence
            }
            
        except ImportError:
            self.logger.warning("PyTorch not available, assuming SD model for .ckpt file")
            return {
                "model_type": ModelType.STABLE_DIFFUSION,
                "model_format": ModelFormat.CKPT,
                "metadata": {"torch_unavailable": True},
                "confidence": 0.7
            }
        except Exception as e:
            self.logger.error(f"Error detecting CKPT model: {e}")
            return {
                "model_type": ModelType.STABLE_DIFFUSION,
                "model_format": ModelFormat.CKPT,
                "metadata": {"error": str(e)},
                "confidence": 0.6
            }
    
    def _classify_state_dict(self, state_dict: Dict[str, Any]) -> Tuple[str, float]:
        """Classify model based on state dict keys"""
        keys = list(state_dict.keys())
        
        # SD-specific patterns
        sd_patterns = ['model.diffusion_model', 'first_stage_model', 'cond_stage_model']
        llm_patterns = ['transformer', 'decoder', 'lm_head']
        
        sd_score = sum(1 for pattern in sd_patterns for key in keys if pattern in key)
        llm_score = sum(1 for pattern in llm_patterns for key in keys if pattern in key)
        
        if sd_score > llm_score:
            return ModelType.STABLE_DIFFUSION, min(0.9, 0.6 + sd_score * 0.1)
        elif llm_score > 0:
            return ModelType.LLM, min(0.8, 0.6 + llm_score * 0.1)
        else:
            # Default to SD for .ckpt files
            return ModelType.STABLE_DIFFUSION, 0.7
