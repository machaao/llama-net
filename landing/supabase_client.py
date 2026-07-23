import os
import hashlib
import secrets
import time
from typing import Dict, Any, Optional, List
from common.utils import get_logger
from landing.node_registry import model_name_to_slug

logger = get_logger(__name__)


class SupabaseManager:
    """Supabase client wrapper for all database operations"""

    def __init__(self):
        from supabase import create_client, Client

        self.url = os.environ.get("SUPABASE_URL", "")
        self.anon_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
        self.service_key = os.environ.get("SUPABASE_SECRET_KEY", "")

        if not self.url or not self.service_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SECRET_KEY must be set")

        self.client: Client = create_client(self.url, self.service_key)
        
        # Node token cache for detecting restarts (in-memory is fine for ephemeral comparison)
        self._node_token_cache: Dict[str, int] = {}  # node_hash -> last known total_tokens
        
        logger.info("Supabase client initialized")

    def get_or_create_user(
        self, user_id: str, email: str, full_name: str = "",
        avatar_url: str = "", google_id: str = ""
    ) -> Dict[str, Any]:
        try:
            result = self.client.table("users").select("*").eq("id", user_id).execute()
            if result.data:
                self.client.table("users").update({"last_login": "now()"}).eq("id", user_id).execute()
                return result.data[0]
            user_data = {
                "id": user_id, "email": email, "full_name": full_name,
                "avatar_url": avatar_url, "google_id": google_id,
            }
            result = self.client.table("users").insert(user_data).execute()
            logger.info(f"Created new user: {email}")
            return result.data[0]
        except Exception as e:
            logger.error(f"Error in get_or_create_user: {e}")
            raise

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        try:
            result = self.client.table("users").select("*").eq("id", user_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting user: {e}")
            return None

    def create_api_key(self, user_id: str, name: str = "default") -> Dict[str, str]:
        raw_key = f"ln-{secrets.token_hex(24)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_prefix = raw_key[:12] + "..."
        self.client.table("api_keys").insert({
            "user_id": user_id, "key_hash": key_hash, "key_prefix": key_prefix,
            "name": name, "is_active": True,
        }).execute()
        logger.info(f"Created API key for user {user_id[:8]}...")
        return {"key": raw_key, "key_prefix": key_prefix}

    def validate_api_key(self, raw_key: str) -> Optional[str]:
        try:
            key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
            result = self.client.table("api_keys").select("*").eq(
                "key_hash", key_hash
            ).eq("is_active", True).execute()
            if result.data:
                self.client.table("api_keys").update({"last_used": "now()"}).eq("key_hash", key_hash).execute()
                return result.data[0]["user_id"]
            return None
        except Exception as e:
            logger.error(f"Error validating API key: {e}")
            return None

    def list_api_keys(self, user_id: str) -> List[Dict[str, Any]]:
        try:
            result = self.client.table("api_keys").select(
                "id, key_prefix, name, last_used, created_at, is_active"
            ).eq("user_id", user_id).order("created_at", desc=True).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error listing API keys: {e}")
            return []

    def revoke_api_key(self, user_id: str, key_id: str) -> bool:
        try:
            result = self.client.table("api_keys").update(
                {"is_active": False}
            ).eq("id", key_id).eq("user_id", user_id).execute()
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Error revoking API key: {e}")
            return False

    def register_node(
        self, user_id: str, node_hash: str, model_name: str,
        model_slug: str, url: str, ip: str = "", port: int = 8000,
        gpu_info: str = "", metrics: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        try:
            metrics = metrics or {}
            node_data = {
                "node_hash": node_hash, "user_id": user_id,
                "model_name": model_name, "model_slug": model_slug,
                "url": url, "ip": ip, "port": port, "gpu_info": gpu_info,
                "load": metrics.get("load", 0), "tps": metrics.get("tps", 0),
                "ttft": metrics.get("ttft"), "latency": metrics.get("latency"),
                "uptime": metrics.get("uptime", 0),
                "total_tokens": metrics.get("total_tokens", 0),
                "status": "active", "last_heartbeat": "now()",
            }
            result = self.client.table("nodes").upsert(node_data, on_conflict="node_hash").execute()
            logger.info(f"Registered node {node_hash[:12]}... model={model_name}")
            return result.data[0] if result.data else node_data
        except Exception as e:
            logger.error(f"Error registering node: {e}")
            raise

    def _get_cumulative_tokens(self) -> int:
        """Get cumulative total tokens from the statistics table."""
        try:
            result = self.client.table("global_statistics").select("value").eq(
                "key", "cumulative_total_tokens"
            ).execute()
            if result.data:
                return int(result.data[0]["value"])
            return 0
        except Exception as e:
            logger.debug(f"Could not read cumulative tokens: {e}")
            return 0

    def _add_cumulative_tokens(self, amount: int) -> None:
        """Atomically add to cumulative total tokens in the statistics table."""
        try:
            current = self._get_cumulative_tokens()
            new_value = current + amount
            self.client.table("global_statistics").upsert(
                {"key": "cumulative_total_tokens", "value": str(new_value)},
                on_conflict="key"
            ).execute()
            logger.debug(f"Accumulated {amount} tokens → cumulative total: {new_value}")
        except Exception as e:
            logger.error(f"Error updating cumulative tokens: {e}")

    def update_node_metrics(self, node_hash: str, metrics: Dict[str, Any]) -> bool:
        try:
            new_tokens = metrics.get("total_tokens", 0)
            old_tokens = self._node_token_cache.get(node_hash, 0)

            # If tokens decreased, the node restarted — accumulate the old value
            if new_tokens < old_tokens and old_tokens > 0:
                self._add_cumulative_tokens(old_tokens)
                logger.info(f"Node {node_hash} restarted — accumulated {old_tokens} tokens to cumulative total")

            # Update cache
            self._node_token_cache[node_hash] = new_tokens

            update_data = {
                "load": metrics.get("load", 0), "tps": metrics.get("tps", 0),
                "ttft": metrics.get("ttft"), "latency": metrics.get("latency"),
                "uptime": metrics.get("uptime", 0),
                "total_tokens": new_tokens,
                "last_heartbeat": "now()", "status": "active",
            }

            # Persist pool_models to metrics column for durability across restarts
            if "pool_models" in metrics:
                update_data["pool_models"] = metrics["pool_models"]

            result = self.client.table("nodes").update(update_data).eq("node_hash", node_hash).execute()
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Error updating node metrics: {e}")
            return False

    def deregister_node(self, node_hash: str) -> bool:
        try:
            # Accumulate tokens before deactivating
            cached = self._node_token_cache.get(node_hash, 0)
            if cached > 0:
                self._add_cumulative_tokens(cached)
                self._node_token_cache.pop(node_hash, None)
                logger.info(f"Accumulated {cached} tokens from departing node {node_hash}")

            result = self.client.table("nodes").update(
                {"status": "inactive"}
            ).eq("node_hash", node_hash).execute()
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Error deregistering node: {e}")
            return False

    def get_user_nodes(self, user_id: str) -> List[Dict[str, Any]]:
        try:
            result = self.client.table("nodes").select("*").eq(
                "user_id", user_id
            ).order("created_at", desc=True).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting user nodes: {e}")
            return []

    def search_nodes(
        self, query: str = "", model_slug: str = "",
        status: str = "active", limit: int = 50
    ) -> List[Dict[str, Any]]:
        try:
            q = self.client.table("nodes").select("*").eq("status", status).limit(limit)
            if model_slug:
                q = q.eq("model_slug", model_slug)
            elif query:
                q = q.ilike("model_name", f"%{query}%")
            result = q.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error searching nodes: {e}")
            return []

    def get_nodes_for_model(self, model_slug: str) -> List[Dict[str, Any]]:
        try:
            result = self.client.table("nodes").select("*").eq(
                "model_slug", model_slug
            ).eq("status", "active").order("load").execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting nodes for model: {e}")
            return []

    def list_active_models(self) -> List[Dict[str, Any]]:
        try:
            result = self.client.table("nodes").select(
                "model_name, model_slug"
            ).eq("status", "active").execute()
            if not result.data:
                return []
            models = {}
            for node in result.data:
                slug = node["model_slug"]
                if slug not in models:
                    models[slug] = {"model_name": node["model_name"], "model_slug": slug, "node_count": 0}
                models[slug]["node_count"] += 1

            # Also discover pool models from metadata AND assign nodes to them
            all_nodes_result = self.client.table("nodes").select(
                "*"
            ).eq("status", "active").execute()
            all_nodes = all_nodes_result.data or []

            if all_nodes:
                from landing.node_registry import model_name_to_slug

                for node in all_nodes:
                    node_hash = node.get("node_hash", "")
                    node_metrics = node.get("metrics", {}) or {}

                    # Read pool_models from persisted DB metrics column
                    all_model_names = node_metrics.get("pool_models", [])

                    # For every model this node can serve (primary + pool),
                    # ensure an entry exists in `models` and add this node to it.
                    seen_slugs = set()
                    for pool_model_name in all_model_names:
                        pool_slug = model_name_to_slug(pool_model_name)
                        if pool_slug in seen_slugs:
                            continue
                        seen_slugs.add(pool_slug)

                        if pool_slug not in models:
                            models[pool_slug] = {
                                "model_name": pool_model_name,
                                "model_slug": pool_slug,
                                "node_count": 0,
                                "pool_discovered": True,
                                "nodes": [],
                            }

                        # Always ensure "nodes" key exists
                        if "nodes" not in models[pool_slug]:
                            models[pool_slug]["nodes"] = []

                        node_hashes = [n.get("node_hash", "") for n in models[pool_slug]["nodes"]]
                        if node_hash not in node_hashes:
                            models[pool_slug]["nodes"].append(node)
                            models[pool_slug]["node_count"] = len(models[pool_slug]["nodes"])

            # Calculate aggregated metrics for every model
            for slug, model in models.items():
                if model.get("pool_discovered"):
                    # Pool models already have their nodes assigned above
                    nodes = model.get("nodes", [])
                else:
                    nodes = self.get_nodes_for_model(slug)
                model["total_tps"] = sum(n.get("tps", 0) for n in nodes)
                model["avg_load"] = sum(n.get("load", 0) for n in nodes) / len(nodes) if nodes else 0
                model["avg_ttft"] = sum(n.get("ttft", 0) or 0 for n in nodes) / len(nodes) if nodes else 0
                model["total_tokens"] = sum(n.get("total_tokens", 0) for n in nodes)
                model["best_node"] = min(nodes, key=lambda n: n.get("load", 1)) if nodes else None
                model["nodes"] = nodes
            # Remove stale model entries
            models = {slug: m for slug, m in models.items() if m["node_count"] > 0}
            return sorted(models.values(), key=lambda m: m["node_count"], reverse=True)
        except Exception as e:
            logger.error(f"Error listing active models: {e}")
            return []

    def cleanup_stale(self, max_age_seconds: int = 120) -> int:
        try:
            result = self.client.table("nodes").select("*").eq("status", "active").execute()
            if not result.data:
                return 0
            now = time.time()
            stale_count = 0
            for node in result.data:
                heartbeat = node.get("last_heartbeat", "")
                if not heartbeat:
                    continue
                from datetime import datetime
                try:
                    hb_time = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
                    age = now - hb_time.timestamp()
                    if age > max_age_seconds:
                        self.client.table("nodes").update(
                            {"status": "inactive"}
                        ).eq("node_hash", node["node_hash"]).execute()
                        stale_count += 1
                except Exception:
                    pass
            if stale_count > 0:
                logger.info(f"Cleaned up {stale_count} stale nodes")
            return stale_count
        except Exception as e:
            logger.error(f"Error cleaning stale nodes: {e}")
            return 0

    def get_network_stats(self) -> Dict[str, Any]:
        try:
            result = self.client.table("nodes").select("*").eq("status", "active").execute()
            nodes = result.data or []
            cumulative = self._get_cumulative_tokens()
            if not nodes:
                return {"total_nodes": 0, "total_models": 0, "total_tps": 0, "avg_load": 0, "total_tokens": cumulative}

            models = set(n["model_slug"] for n in nodes)

            # Also count pool models from persisted DB metrics column
            for n in nodes:
                node_metrics = n.get("metrics", {}) or {}
                for model_name in node_metrics.get("pool_models", []):
                    slug = model_name_to_slug(model_name)
                    models.add(slug)

            active_tokens = sum(n.get("total_tokens", 0) for n in nodes)
            return {
                "total_nodes": len(nodes), "total_models": len(models),
                "total_tps": round(sum(n.get("tps", 0) for n in nodes), 1),
                "avg_load": round(sum(n.get("load", 0) for n in nodes) / len(nodes), 3),
                "total_tokens": cumulative + active_tokens,
            }
        except Exception as e:
            logger.error(f"Error getting network stats: {e}")
            return {"total_nodes": 0, "total_models": 0, "total_tps": 0, "avg_load": 0, "total_tokens": 0}

    def store_tunnel(self, node_hash: str, tunnel_id: str, tunnel_token: str, hostname: str, dns_record_id: str = "") -> bool:
        try:
            self.client.table("tunnel_state").upsert({
                "node_hash": node_hash, "tunnel_id": tunnel_id,
                "tunnel_token": tunnel_token, "hostname": hostname,
                "dns_record_id": dns_record_id,
            }, on_conflict="node_hash").execute()
            return True
        except Exception as e:
            logger.error(f"Error storing tunnel: {e}")
            return False

    def get_tunnel(self, node_hash: str) -> Optional[Dict[str, Any]]:
        try:
            result = self.client.table("tunnel_state").select("*").eq("node_hash", node_hash).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting tunnel: {e}")
            return None

    def delete_tunnel(self, node_hash: str) -> bool:
        try:
            self.client.table("tunnel_state").delete().eq("node_hash", node_hash).execute()
            return True
        except Exception as e:
            logger.error(f"Error deleting tunnel: {e}")
            return False
