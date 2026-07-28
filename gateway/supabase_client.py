import os
import json
import hashlib
import secrets
import time
from typing import Dict, Any, Optional, List
from common.utils import get_logger
from gateway.node_registry import model_name_to_slug

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
        
        # Node token cache for detecting restarts
        self._node_token_cache: Dict[str, int] = {}  # node_hash -> last known total_tokens
        self._hydrate_token_cache()
        
        logger.info("Supabase client initialized")

    def _hydrate_token_cache(self):
        """Load current total_tokens from active nodes + persisted cache on startup.
        
        Ensures gateway restart doesn't lose restart-detection state.
        """
        try:
            # 1. Load persisted cache snapshot (survives gateway restart)
            result = self.client.table("global_statistics").select("value").eq(
                "key", "node_token_cache"
            ).execute()
            if result.data:
                persisted = json.loads(result.data[0]["value"])
                if isinstance(persisted, dict):
                    self._node_token_cache.update(persisted)
                    logger.info(f"Hydrated token cache from persistence: {len(persisted)} entries")

            # 2. Overlay with current active node data (more recent)
            result = self.client.table("nodes").select(
                "node_hash, total_tokens"
            ).eq("status", "active").execute()
            for row in (result.data or []):
                self._node_token_cache[row["node_hash"]] = row.get("total_tokens", 0)

            logger.info(f"Token cache ready: {len(self._node_token_cache)} nodes tracked")
        except Exception as e:
            logger.warning(f"Could not hydrate token cache: {e}")

    def _persist_token_cache(self):
        """Persist token cache snapshot so gateway restart doesn't lose it."""
        try:
            self.client.table("global_statistics").upsert({
                "key": "node_token_cache",
                "value": json.dumps(self._node_token_cache),
                "updated_at": "now()",
            }, on_conflict="key").execute()
        except Exception as e:
            logger.debug(f"Could not persist token cache: {e}")

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

    def record_token_usage(self, key_hash: str, tokens: int) -> None:
        """Atomically increment token usage for an API key today."""
        try:
            from datetime import date
            today = date.today().isoformat()

            result = self.client.table("token_usage").select("*").eq(
                "key_hash", key_hash
            ).eq("usage_date", today).execute()

            if result.data:
                current = result.data[0]
                self.client.table("token_usage").update({
                    "tokens_consumed": current["tokens_consumed"] + tokens,
                    "requests_count": current["requests_count"] + 1,
                }).eq("key_hash", key_hash).eq("usage_date", today).execute()
            else:
                self.client.table("token_usage").insert({
                    "key_hash": key_hash,
                    "usage_date": today,
                    "tokens_consumed": tokens,
                    "requests_count": 1,
                }).execute()
        except Exception as e:
            logger.error(f"Error recording token usage: {e}")

    def get_token_usage(self, key_hash: str) -> dict:
        """Get today's token usage for an API key."""
        try:
            from datetime import date
            today = date.today().isoformat()
            result = self.client.table("token_usage").select("*").eq(
                "key_hash", key_hash
            ).eq("usage_date", today).execute()
            if result.data:
                return result.data[0]
            return {"key_hash": key_hash, "date": today, "tokens_consumed": 0, "requests_count": 0}
        except Exception as e:
            logger.error(f"Error getting token usage: {e}")
            return {"key_hash": key_hash, "tokens_consumed": 0, "requests_count": 0}

    def get_daily_token_budget(self) -> int:
        """Get the daily token budget per API key from env or default."""
        return int(os.environ.get("LLAMANET_DAILY_TOKEN_BUDGET", "500000"))

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
        gpu_info: str = "", metrics: Dict[str, Any] = None,
        ctx_length: int = 0, models_list: list = None,
    ) -> Dict[str, Any]:
        try:
            metrics = metrics or {}
            node_data = {
                "node_hash": node_hash, "user_id": user_id,
                "url": url, "ip": ip, "port": port, "gpu_info": gpu_info,
                "load": metrics.get("load", 0), "tps": metrics.get("tps", 0),
                "ttft": metrics.get("ttft"), "latency": metrics.get("latency"),
                "uptime": metrics.get("uptime", 0),
                "total_tokens": metrics.get("total_tokens", 0),
                "ctx_length": ctx_length,
                "status": "active", "last_heartbeat": "now()",
            }
            result = self.client.table("nodes").upsert(node_data, on_conflict="node_hash").execute()

            # Upsert node_models junction table
            all_models = models_list or [{"name": model_name, "ctx_length": ctx_length}]
            self.upsert_node_models(node_hash, all_models, model_slug)

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

            # Persist pool_models and upsert node_models
            if "pool_models" in metrics:
                pool_models = metrics["pool_models"]
                update_data["pool_models"] = pool_models
                update_data["metrics"] = {"pool_models": pool_models}
                # Extract active model slug from node_models junction table
                active_slug = ""
                try:
                    nm_result = self.client.table("node_models").select("model_slug").eq(
                        "node_hash", node_hash
                    ).eq("is_active", True).eq("status", "active").execute()
                    if nm_result.data:
                        active_slug = nm_result.data[0].get("model_slug", "")
                except Exception:
                    pass
                self.upsert_node_models(node_hash, pool_models, active_slug)
                # Use ctx_length from first pool model or direct metric
                if pool_models and isinstance(pool_models[0], dict):
                    update_data["ctx_length"] = pool_models[0].get("ctx_length", 0)

            # Also accept ctx_length directly from metrics (for event payload)
            if "ctx_length" in metrics and isinstance(metrics["ctx_length"], int):
                update_data["ctx_length"] = metrics["ctx_length"]

            result = self.client.table("nodes").update(update_data).eq("node_hash", node_hash).execute()

            # Persist token cache for gateway restart resilience
            self._persist_token_cache()

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
                self._persist_token_cache()
                logger.info(f"Accumulated {cached} tokens from departing node {node_hash}")

            result = self.client.table("nodes").update(
                {"status": "inactive"}
            ).eq("node_hash", node_hash).execute()

            # Mark all node_models as evicted
            try:
                self.client.table("node_models").update({
                    "status": "evicted",
                    "is_active": False,
                    "updated_at": "now()",
                }).eq("node_hash", node_hash).eq("status", "active").execute()
            except Exception as e:
                logger.debug(f"Error evicting node_models: {e}")

            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Error deregistering node: {e}")
            return False

    def upsert_node_models(self, node_hash: str, models_list: list, active_model_slug: str = "") -> None:
        """Upsert node_models entries for a node's pool.

        models_list: [{"name": "model-a", "ctx_length": 8192}, ...]
        Handles backward compat: if item is a string, wraps it.
        """
        try:
            from gateway.node_registry import model_name_to_slug

            for model in models_list:
                if isinstance(model, str):
                    model_name = model
                    ctx_length = 0
                elif isinstance(model, dict):
                    model_name = model.get("name", model.get("model_name", ""))
                    ctx_length = model.get("ctx_length", 0)
                else:
                    continue

                if not model_name:
                    continue

                model_slug = model_name_to_slug(model_name)
                is_active = (model_slug == active_model_slug)

                self.client.table("node_models").upsert({
                    "node_hash": node_hash,
                    "model_name": model_name,
                    "model_slug": model_slug,
                    "ctx_length": ctx_length,
                    "is_active": is_active,
                    "status": "active",
                    "updated_at": "now()",
                }, on_conflict="node_hash,model_slug").execute()

            logger.debug(f"Upserted {len(models_list)} node_models for {node_hash}")
        except Exception as e:
            logger.error(f"Error upserting node_models: {e}")

    def get_node_models(self, node_hash: str, status: str = "active") -> list:
        """Get model entries for a node, filtered by status."""
        try:
            q = self.client.table("node_models").select("*").eq(
                "node_hash", node_hash
            )
            if status:
                q = q.eq("status", status)
            result = q.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting node_models: {e}")
            return []

    def evict_stale_node_models(self, node_hash: str, valid_slugs: list) -> None:
        """Mark node_models entries no longer in the pool as evicted."""
        try:
            if not valid_slugs:
                return
            result = self.client.table("node_models").select("model_slug").eq(
                "node_hash", node_hash
            ).eq("status", "active").execute()
            if not result.data:
                return
            for row in result.data:
                if row["model_slug"] not in valid_slugs:
                    self.client.table("node_models").update({
                        "status": "evicted",
                        "is_active": False,
                        "updated_at": "now()",
                    }).eq(
                        "node_hash", node_hash
                    ).eq("model_slug", row["model_slug"]).execute()
                    logger.debug(f"Evicted stale node_model {row['model_slug']} for {node_hash}")
        except Exception as e:
            logger.error(f"Error evicting stale node_models: {e}")

    def get_user_nodes(self, user_id: str) -> List[Dict[str, Any]]:
        try:
            result = self.client.table("nodes").select("*").eq(
                "user_id", user_id
            ).order("created_at", desc=True).execute()
            nodes = result.data or []
            # Enrich with model_name from node_models junction table
            for node in nodes:
                node_hash = node.get("node_hash", "")
                if node_hash:
                    try:
                        nm = self.client.table("node_models").select("model_name").eq(
                            "node_hash", node_hash
                        ).eq("is_active", True).eq("status", "active").execute()
                        if nm.data:
                            node["model_name"] = nm.data[0].get("model_name", "unknown")
                        else:
                            node["model_name"] = "unknown"
                    except Exception:
                        node["model_name"] = "unknown"
            return nodes
        except Exception as e:
            logger.error(f"Error getting user nodes: {e}")
            return []

    def search_nodes(
        self, query: str = "", model_slug: str = "",
        status: str = "active", limit: int = 50
    ) -> List[Dict[str, Any]]:
        try:
            if model_slug:
                nm = self.client.table("node_models").select("node_hash").eq(
                    "model_slug", model_slug
                ).eq("status", "active").execute()
                hashes = [r["node_hash"] for r in (nm.data or [])]
                if not hashes:
                    return []
                q = self.client.table("nodes").select("*").in_(
                    "node_hash", hashes
                ).eq("status", status).limit(limit)
            elif query:
                nm = self.client.table("node_models").select("node_hash").ilike(
                    "model_name", f"%{query}%"
                ).eq("status", "active").execute()
                hashes = [r["node_hash"] for r in (nm.data or [])]
                if not hashes:
                    return []
                q = self.client.table("nodes").select("*").in_(
                    "node_hash", hashes
                ).eq("status", status).limit(limit)
            else:
                q = self.client.table("nodes").select("*").eq(
                    "status", status
                ).limit(limit)
            result = q.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error searching nodes: {e}")
            return []

    def get_nodes_for_model(self, model_slug: str) -> List[Dict[str, Any]]:
        try:
            nm_result = self.client.table("node_models").select(
                "node_hash, ctx_length"
            ).eq("model_slug", model_slug).eq("status", "active").execute()

            if not nm_result.data:
                return []

            node_hashes = [r["node_hash"] for r in nm_result.data]
            ctx_map = {r["node_hash"]: r.get("ctx_length", 0) for r in nm_result.data}

            nodes_result = self.client.table("nodes").select("*").in_(
                "node_hash", node_hashes
            ).eq("status", "active").order("load").execute()

            for node in (nodes_result.data or []):
                node["ctx_length"] = ctx_map.get(node["node_hash"], 0)

            return nodes_result.data or []
        except Exception as e:
            logger.error(f"Error getting nodes for model: {e}")
            return []

    def list_active_models(self) -> List[Dict[str, Any]]:
        try:
            from gateway.node_registry import model_name_to_slug

            # ── Primary source: node_models junction table ──
            try:
                nm_result = self.client.table("node_models").select(
                    "node_hash, model_name, model_slug, ctx_length, is_active"
                ).eq("status", "active").execute()

                if nm_result.data:
                    models: Dict[str, Dict] = {}
                    node_hashes_needed = set()

                    for nm in nm_result.data:
                        slug = nm["model_slug"]
                        node_hashes_needed.add(nm["node_hash"])
                        if slug not in models:
                            models[slug] = {
                                "model_name": nm["model_name"],
                                "model_slug": slug,
                                "node_count": 0,
                                "nodes": [],
                            }
                        models[slug]["nodes"].append({
                            "node_hash": nm["node_hash"],
                            "ctx_length": nm.get("ctx_length", 0),
                            "is_active": nm.get("is_active", False),
                        })
                        models[slug]["node_count"] = len(models[slug]["nodes"])

                    if node_hashes_needed:
                        nodes_result = self.client.table("nodes").select("*").eq(
                            "status", "active"
                        ).in_("node_hash", list(node_hashes_needed)).execute()
                        nodes_by_hash = {
                            n["node_hash"]: n for n in (nodes_result.data or [])
                        }
                    else:
                        nodes_by_hash = {}

                    for slug, model in models.items():
                        enriched_nodes = []
                        for nm_node in model["nodes"]:
                            full_node = nodes_by_hash.get(nm_node["node_hash"])
                            if not full_node or full_node.get("status") != "active":
                                continue
                            full_node["ctx_length"] = nm_node["ctx_length"]
                            enriched_nodes.append(full_node)

                        model["nodes"] = enriched_nodes
                        model["node_count"] = len(enriched_nodes)
                        model["total_tps"] = sum(n.get("tps", 0) for n in enriched_nodes)
                        model["avg_load"] = (
                            sum(n.get("load", 0) for n in enriched_nodes) / len(enriched_nodes)
                            if enriched_nodes else 0
                        )
                        model["avg_ttft"] = (
                            sum(n.get("ttft", 0) or 0 for n in enriched_nodes) / len(enriched_nodes)
                            if enriched_nodes else 0
                        )
                        model["total_tokens"] = sum(n.get("total_tokens", 0) for n in enriched_nodes)
                        model["best_node"] = (
                            min(enriched_nodes, key=lambda n: n.get("load", 1))
                            if enriched_nodes else None
                        )

                    models = {s: m for s, m in models.items() if m["node_count"] > 0}
                    return sorted(models.values(), key=lambda m: m["node_count"], reverse=True)

            except Exception as e:
                logger.warning(f"node_models query failed: {e}")
                return []
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
                        try:
                            self.client.table("node_models").delete().eq(
                                "node_hash", node["node_hash"]
                            ).execute()
                        except Exception:
                            pass
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

            # Count models from node_models junction table
            try:
                nm_result = self.client.table("node_models").select("model_slug").eq("status", "active").execute()
                models = set(r["model_slug"] for r in (nm_result.data or []))
            except Exception:
                models = set()

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
