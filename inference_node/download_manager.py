import os
import asyncio
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, field
from enum import Enum
from common.utils import get_logger
from inference_node.hf_downloader import HFModelDownloader

logger = get_logger(__name__)

class DownloadStatus(Enum):
    PENDING = "pending"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class DownloadTask:
    download_id: str
    repo_id: str
    quantization: Optional[str]
    status: DownloadStatus = DownloadStatus.PENDING
    bytes_downloaded: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    error_message: Optional[str] = None
    file_path: Optional[str] = None
    started_at: float = 0.0
    completed_at: float = 0.0
    progress_queues: list = field(default_factory=list)


class DownloadManager:
    """Manages model downloads with progress tracking and SSE support"""

    def __init__(self, cache_dir: Optional[str] = None):
        self.downloader = HFModelDownloader(cache_dir)
        self.cache_dir = self.downloader.cache_dir
        self.active_downloads: Dict[str, DownloadTask] = {}
        self.download_history: Dict[str, DownloadTask] = {}
        self._download_lock = asyncio.Lock()
        self._cancel_flags: Dict[str, bool] = {}

    async def search_models(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Search Hugging Face for GGUF models. Empty query returns trending models."""
        import requests as req
        try:
            # Build search query - ensure "gguf" is in the search terms
            if query and query.strip():
                search_term = f"{query} gguf"
            else:
                search_term = "gguf"

            url = "https://huggingface.co/api/models"
            params = {
                "search": search_term,
                "sort": "trendingScore",
                "direction": "-1",
                "limit": limit * 2,  # Fetch extra since we filter client-side
            }

            response = req.get(url, params=params, timeout=30)
            response.raise_for_status()
            models = response.json()

            # Filter client-side for models that have GGUF tags or gguf in the name
            results = []
            for model in models:
                model_id = model.get("modelId", model.get("id", ""))
                tags = [t.lower() for t in model.get("tags", [])]
                has_gguf_tag = "gguf" in tags or "gguf" in model_id.lower()

                if not has_gguf_tag:
                    continue

                results.append({
                    "repo_id": model_id,
                    "downloads": model.get("downloads", 0),
                    "likes": model.get("likes", 0),
                    "tags": model.get("tags", []),
                    "last_modified": model.get("lastModified", ""),
                    "pipeline_tag": model.get("pipeline_tag", ""),
                })

                if len(results) >= limit:
                    break

            return results
        except Exception as e:
            logger.error(f"Error searching models: {e}")
            return []

    async def get_model_details(self, repo_id: str) -> Dict[str, Any]:
        """Get detailed model info including GGUF files"""
        try:
            loop = asyncio.get_event_loop()
            model_info = await loop.run_in_executor(
                None, self.downloader.get_model_info, repo_id
            )
            return model_info
        except Exception as e:
            logger.error(f"Error getting model details for {repo_id}: {e}")
            return {"repo_id": repo_id, "gguf_files": [], "error": str(e)}

    async def start_download(self, repo_id: str, quantization: str = "Q4_K_M") -> str:
        """Start downloading a model, returns download_id"""
        download_id = f"dl_{uuid.uuid4().hex[:8]}"

        task = DownloadTask(
            download_id=download_id,
            repo_id=repo_id,
            quantization=quantization,
            status=DownloadStatus.PENDING,
            started_at=time.time(),
        )
        self.active_downloads[download_id] = task
        self._cancel_flags[download_id] = False

        # Start download in background
        asyncio.create_task(self._execute_download(task))

        logger.info(f"Download queued: {download_id} for {repo_id}:{quantization}")
        return download_id

    async def _execute_download(self, task: DownloadTask):
        """Execute the actual download with progress tracking"""
        try:
            async with self._download_lock:
                task.status = DownloadStatus.SEARCHING
                await self._broadcast_progress(task, "searching")

                # Find the appropriate GGUF file
                loop = asyncio.get_event_loop()
                model_info = await loop.run_in_executor(
                    None, self.downloader.get_model_info, task.repo_id
                )

                if not model_info.get("gguf_files"):
                    task.status = DownloadStatus.FAILED
                    task.error_message = f"No GGUF files found in {task.repo_id}"
                    await self._broadcast_progress(task, "error")
                    return

                # Find best matching quantization
                filename = None
                for f in model_info["gguf_files"]:
                    if task.quantization and task.quantization.upper() in f.upper():
                        filename = f
                        break

                if not filename:
                    preferred = ["Q4_K_M", "Q4_K_S", "Q5_K_M", "Q8_0"]
                    for pref in preferred:
                        for f in model_info["gguf_files"]:
                            if pref in f.upper():
                                filename = f
                                break
                        if filename:
                            break

                if not filename:
                    filename = model_info["gguf_files"][0]

                logger.info(f"Selected GGUF file: {filename}")

                hf_url = f"{task.repo_id}:{task.quantization}"
                task.status = DownloadStatus.DOWNLOADING
                await self._broadcast_progress(task, "downloading")

                file_path = await self._download_with_progress(task, hf_url)

                if file_path and not self._cancel_flags.get(task.download_id):
                    task.status = DownloadStatus.COMPLETED
                    task.file_path = file_path
                    task.completed_at = time.time()
                    await self._broadcast_progress(task, "completed")
                    logger.info(f"Download completed: {task.download_id} -> {file_path}")
                elif self._cancel_flags.get(task.download_id):
                    task.status = DownloadStatus.CANCELLED
                    await self._broadcast_progress(task, "cancelled")
                else:
                    task.status = DownloadStatus.FAILED
                    task.error_message = "Download failed - no file produced"
                    await self._broadcast_progress(task, "error")

        except Exception as e:
            task.status = DownloadStatus.FAILED
            task.error_message = str(e)
            await self._broadcast_progress(task, "error")
            logger.error(f"Download failed for {task.download_id}: {e}")

        finally:
            self.download_history[task.download_id] = task

    async def _download_with_progress(self, task: DownloadTask, hf_url: str) -> Optional[str]:
        """Download model with progress callbacks"""
        import requests as req

        try:
            repo_id, _, tag = self.downloader.parse_hf_url(hf_url)

            quantization = None
            if tag and __import__('re').match(r'^Q[0-9]+_[A-Z0-9_]+$', tag):
                quantization = tag
                tag = None

            filename = self.downloader.find_gguf_file(repo_id, tag, quantization)

            local_path = self.downloader.get_local_model_path(
                repo_id, filename, tag, quantization
            )

            if local_path.exists():
                task.bytes_downloaded = local_path.stat().st_size
                task.total_bytes = task.bytes_downloaded
                return str(local_path)

            download_url = f"https://huggingface.co/{repo_id}/resolve/{tag or 'main'}/{filename}"

            response = req.get(download_url, stream=True, timeout=30)
            response.raise_for_status()

            task.total_bytes = int(response.headers.get('content-length', 0))
            task.bytes_downloaded = 0
            last_broadcast = time.time()
            last_bytes = 0

            local_path.parent.mkdir(parents=True, exist_ok=True)

            with open(local_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=65536):
                    if self._cancel_flags.get(task.download_id):
                        logger.info(f"Download cancelled: {task.download_id}")
                        f.close()
                        local_path.unlink(missing_ok=True)
                        return None

                    if chunk:
                        f.write(chunk)
                        task.bytes_downloaded += len(chunk)

                        now = time.time()
                        if now - last_broadcast >= 0.5:
                            elapsed = now - last_broadcast
                            task.speed = (task.bytes_downloaded - last_bytes) / elapsed
                            last_bytes = task.bytes_downloaded
                            last_broadcast = now
                            await self._broadcast_progress(task, "progress")

            return str(local_path)

        except Exception as e:
            logger.error(f"Download error: {e}")
            task.error_message = str(e)
            return None

    async def _broadcast_progress(self, task: DownloadTask, event_type: str):
        """Broadcast progress to all registered SSE queues"""
        event_data = {
            "type": event_type,
            "download_id": task.download_id,
            "repo_id": task.repo_id,
            "quantization": task.quantization,
            "status": task.status.value,
            "bytes_downloaded": task.bytes_downloaded,
            "total_bytes": task.total_bytes,
            "speed": task.speed,
            "percent": round((task.bytes_downloaded / task.total_bytes * 100), 1) if task.total_bytes > 0 else 0,
            "file_path": task.file_path,
            "error": task.error_message,
            "timestamp": time.time()
        }

        dead_queues = []
        for queue in task.progress_queues:
            try:
                queue.put_nowait(event_data)
            except asyncio.QueueFull:
                dead_queues.append(queue)

        for q in dead_queues:
            task.progress_queues.remove(q)

    def register_progress_listener(self, download_id: str) -> Optional[asyncio.Queue]:
        """Register an SSE listener for download progress"""
        task = self.active_downloads.get(download_id) or self.download_history.get(download_id)
        if not task:
            return None

        queue = asyncio.Queue(maxsize=100)
        task.progress_queues.append(queue)
        return queue

    async def cancel_download(self, download_id: str) -> bool:
        """Cancel an active download"""
        if download_id in self.active_downloads:
            self._cancel_flags[download_id] = True
            logger.info(f"Cancel requested for download: {download_id}")
            return True
        return False

    def get_download_status(self, download_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a specific download"""
        task = self.active_downloads.get(download_id) or self.download_history.get(download_id)
        if not task:
            return None

        return {
            "download_id": task.download_id,
            "repo_id": task.repo_id,
            "quantization": task.quantization,
            "status": task.status.value,
            "bytes_downloaded": task.bytes_downloaded,
            "total_bytes": task.total_bytes,
            "speed": task.speed,
            "percent": round((task.bytes_downloaded / task.total_bytes * 100), 1) if task.total_bytes > 0 else 0,
            "file_path": task.file_path,
            "error": task.error_message,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
        }

    def get_all_downloads(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all downloads (active + history)"""
        all_downloads = {}
        for did, task in {**self.active_downloads, **self.download_history}.items():
            all_downloads[did] = self.get_download_status(did)
        return all_downloads

    def list_local_models(self) -> List[Dict[str, Any]]:
        """List all locally cached models"""
        models = []
        metadata = self.downloader.metadata

        for key, info in metadata.items():
            local_path = Path(info.get("local_path", ""))
            exists = local_path.exists()
            size = local_path.stat().st_size if exists else 0

            models.append({
                "id": key,
                "repo_id": info.get("repo_id", ""),
                "filename": info.get("filename", ""),
                "quantization": info.get("quantization", ""),
                "local_path": str(local_path),
                "exists": exists,
                "size_bytes": size,
                "size_gb": round(size / (1024**3), 2),
                "download_time": info.get("download_time", ""),
            })

        for item in self.cache_dir.rglob("*.gguf"):
            already_listed = any(m["local_path"] == str(item) for m in models)
            if not already_listed:
                size = item.stat().st_size
                models.append({
                    "id": str(item.relative_to(self.cache_dir)),
                    "repo_id": "unknown",
                    "filename": item.name,
                    "quantization": "",
                    "local_path": str(item),
                    "exists": True,
                    "size_bytes": size,
                    "size_gb": round(size / (1024**3), 2),
                    "download_time": "",
                })

        return models

    def delete_local_model(self, model_id: str) -> bool:
        """Delete a locally cached model"""
        try:
            if model_id in self.downloader.metadata:
                info = self.downloader.metadata[model_id]
                local_path = Path(info.get("local_path", ""))
                if local_path.exists():
                    local_path.unlink()
                del self.downloader.metadata[model_id]
                self.downloader._save_metadata()
                return True

            full_path = self.cache_dir / model_id
            if full_path.exists():
                full_path.unlink()
                return True

            return False
        except Exception as e:
            logger.error(f"Error deleting model {model_id}: {e}")
            return False

    def get_disk_usage(self) -> Dict[str, Any]:
        """Get disk usage info for the models cache"""
        total_size = 0
        model_count = 0

        for item in self.cache_dir.rglob("*.gguf"):
            total_size += item.stat().st_size
            model_count += 1

        return {
            "cache_dir": str(self.cache_dir),
            "model_count": model_count,
            "total_size_bytes": total_size,
            "total_size_gb": round(total_size / (1024**3), 2),
        }
