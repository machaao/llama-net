"""
Per-node authentication for gateway→node request forwarding.

The gateway issues a unique bearer token to each node at registration.
The node stores the token in memory and validates it on incoming requests.
No shared secret is ever distributed to nodes.
"""

import os
import secrets
import time
from typing import Optional, Tuple
from common.utils import get_logger

logger = get_logger(__name__)


# ── Gateway-side: Token management ──────────────────────────────


class NodeTokenManager:
    """Manages per-node bearer tokens on the gateway side.

    Tokens are stored in-memory and in Supabase for persistence.
    Each node gets a unique, random 48-byte hex token.
    """

    def __init__(self, supabase_manager=None):
        self._db = supabase_manager
        # In-memory cache: node_hash -> token (avoids DB reads on every forward)
        self._token_cache: dict = {}

    def generate_token(self, node_hash: str) -> str:
        """Generate a new bearer token for a node. Called at registration."""
        token = f"lnn_{secrets.token_hex(24)}"
        self._token_cache[node_hash] = token

        # Persist to Supabase
        if self._db:
            try:
                self._db.client.table("nodes").update(
                    {"node_token": token}
                ).eq("node_hash", node_hash).execute()
            except Exception as e:
                logger.warning(f"Could not persist node token: {e}")

        logger.info(f"Generated bearer token for node {node_hash}")
        return token

    def get_token(self, node_hash: str) -> Optional[str]:
        """Get the current token for a node."""
        if node_hash in self._token_cache:
            return self._token_cache[node_hash]

        # Fallback to DB
        if self._db:
            try:
                result = self._db.client.table("nodes").select("node_token").eq(
                    "node_hash", node_hash
                ).execute()
                if result.data and result.data[0].get("node_token"):
                    token = result.data[0]["node_token"]
                    self._token_cache[node_hash] = token
                    return token
            except Exception as e:
                logger.debug(f"Could not fetch node token from DB: {e}")

        return None

    def revoke_token(self, node_hash: str) -> None:
        """Revoke a node's token (called at deregistration)."""
        self._token_cache.pop(node_hash, None)
        if self._db:
            try:
                self._db.client.table("nodes").update(
                    {"node_token": None}
                ).eq("node_hash", node_hash).execute()
            except Exception as e:
                logger.warning(f"Could not revoke node token: {e}")


# ── Node-side: Token storage and validation ─────────────────────

_node_bearer_token: Optional[str] = None


def set_node_token(token: str) -> None:
    """Store the bearer token received from gateway registration."""
    global _node_bearer_token
    _node_bearer_token = token
    logger.info("Node bearer token stored for gateway authentication")


def verify_node_request(auth_header: str) -> Tuple[bool, str]:
    """Verify an incoming request has a valid gateway bearer token.

    Called by the inference node on every /v1/chat/completions and
    /v1/completions request.

    Returns (is_valid, reason).
    """
    global _node_bearer_token

    # No token stored — either not registered yet or local dev mode
    if not _node_bearer_token:
        return True, "no token configured (local dev or not yet registered)"

    if not auth_header:
        return False, "missing Authorization header — direct access not allowed"

    if auth_header.startswith("Bearer "):
        presented = auth_header[7:]
    else:
        presented = auth_header

    if presented == _node_bearer_token:
        return True, "valid"

    return False, "invalid bearer token"
