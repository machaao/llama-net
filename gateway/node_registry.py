import os
import hashlib
import re
import time
import aiohttp
from typing import Dict, Any, Optional, List
from common.utils import get_logger

logger = get_logger(__name__)


def model_name_to_slug(model_name: str) -> str:
    slug = model_name.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')


class CloudflareClient:
    def __init__(self):
        self.api_token = os.environ.get("CF_API_TOKEN", "")
        self.account_id = os.environ.get("CF_ACCOUNT_ID", "")
        self.zone_id = os.environ.get("CF_ZONE_ID", "")
        self.domain = os.environ.get("CF_TUNNEL_DOMAIN", "llamanet.app")

    @property
    def is_configured(self) -> bool:
        return bool(self.api_token and self.account_id and self.zone_id)

    async def create_tunnel(self, name: str) -> Optional[Dict[str, str]]:
        if not self.is_configured:
            return None
        try:
            headers = {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/cfd_tunnel",
                    headers=headers, json={"name": name, "tunnel_secret": os.urandom(32).hex()},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("success"):
                            return {"tunnel_id": data["result"]["id"], "tunnel_token": data["result"]["token"]}
                    return None
        except Exception as e:
            logger.error(f"Error creating CF tunnel: {e}")
            return None

    async def delete_tunnel(self, tunnel_id: str) -> bool:
        if not self.is_configured:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}",
                    headers={"Authorization": f"Bearer {self.api_token}"},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.error(f"Error deleting CF tunnel: {e}")
            return False

    async def create_dns(self, subdomain: str, tunnel_id: str) -> Optional[str]:
        if not self.is_configured:
            return None
        try:
            headers = {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"https://api.cloudflare.com/client/v4/zones/{self.zone_id}/dns_records",
                    headers=headers,
                    json={"type": "CNAME", "name": subdomain, "content": f"{tunnel_id}.cfargotunnel.com", "proxied": True, "ttl": 1},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("success"):
                            return data["result"]["id"]
                    return None
        except Exception as e:
            logger.error(f"Error creating DNS: {e}")
            return None

    async def delete_dns(self, record_id: str) -> bool:
        if not self.is_configured:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"https://api.cloudflare.com/client/v4/zones/{self.zone_id}/dns_records/{record_id}",
                    headers={"Authorization": f"Bearer {self.api_token}"},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.error(f"Error deleting DNS: {e}")
            return False


class NodeRegistry:
    def __init__(self, supabase_manager, cf_client: CloudflareClient):
        self.db = supabase_manager
        self.cf = cf_client

    def _generate_node_hash(self, node_id: str) -> str:
        return hashlib.sha256(node_id.encode()).hexdigest()[:12]

    async def register_node(
        self, user_id: str, node_id: str, model: str,
        url: str, ip: str = "", port: int = 8000,
        gpu: str = "", metrics: Dict[str, Any] = None,
        enable_tunnel: bool = False,
        ctx_length: int = 0, models_list: list = None,
    ) -> Dict[str, Any]:
        node_hash = self._generate_node_hash(node_id)
        model_slug = model_name_to_slug(model)
        result = {"node_hash": node_hash, "model": model, "url": url, "tunnel_url": None, "tunnel_provisioned": False, "ctx_length": ctx_length}
        self.db.register_node(
            user_id=user_id, node_hash=node_hash, model_name=model,
            model_slug=model_slug, url=url, ip=ip, port=port,
            gpu_info=gpu, metrics=metrics,
            ctx_length=ctx_length, models_list=models_list,
        )
        if enable_tunnel and self.cf.is_configured:
            tunnel_result = await self._provision_tunnel(node_hash, node_id, port)
            if tunnel_result:
                result["tunnel_url"] = tunnel_result["url"]
                result["tunnel_provisioned"] = True
                self.db.register_node(
                    user_id=user_id, node_hash=node_hash, model_name=model,
                    model_slug=model_slug, url=tunnel_result["url"],
                    ip=ip, port=port, gpu_info=gpu, metrics=metrics,
                    ctx_length=ctx_length, models_list=models_list,
                )
        logger.info(f"Registered node {node_hash} model={model} tunnel={result['tunnel_provisioned']}")
        return result

    async def _provision_tunnel(self, node_hash: str, node_id: str, local_port: int) -> Optional[Dict[str, str]]:
        try:
            tunnel_name = f"llamanet-{node_hash}"
            hostname = f"node-{node_hash}.{self.cf.domain}"
            tunnel = await self.cf.create_tunnel(tunnel_name)
            if not tunnel:
                return None
            dns_record_id = await self.cf.create_dns(f"node-{node_hash}", tunnel["tunnel_id"])
            self.db.store_tunnel(
                node_hash=node_hash, tunnel_id=tunnel["tunnel_id"],
                tunnel_token=tunnel["tunnel_token"], hostname=hostname,
                dns_record_id=dns_record_id or "",
            )
            return {"url": f"https://{hostname}", "tunnel_id": tunnel["tunnel_id"], "tunnel_token": tunnel["tunnel_token"], "hostname": hostname}
        except Exception as e:
            logger.error(f"Error provisioning tunnel: {e}")
            return None

    async def heartbeat(self, node_hash: str, metrics: Dict[str, Any]) -> bool:
        return self.db.update_node_metrics(node_hash, metrics)

    async def deregister(self, node_hash: str) -> bool:
        try:
            tunnel = self.db.get_tunnel(node_hash)
            if tunnel:
                if tunnel.get("dns_record_id"):
                    await self.cf.delete_dns(tunnel["dns_record_id"])
                if tunnel.get("tunnel_id"):
                    await self.cf.delete_tunnel(tunnel["tunnel_id"])
                self.db.delete_tunnel(node_hash)
            return self.db.deregister_node(node_hash)
        except Exception as e:
            logger.error(f"Error deregistering node: {e}")
            return False

    async def cleanup_stale(self) -> int:
        stale_nodes = self.db.search_nodes(status="active", limit=500)
        now = time.time()
        cleaned = 0
        for node in stale_nodes:
            heartbeat = node.get("last_heartbeat", "")
            if not heartbeat:
                continue
            from datetime import datetime
            try:
                hb_time = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
                if now - hb_time.timestamp() > 120:
                    await self.deregister(node["node_hash"])
                    cleaned += 1
            except Exception:
                pass
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} stale nodes")
        return cleaned
