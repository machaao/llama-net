import os
import hashlib
import secrets
from typing import Optional, Dict, Any, Callable
from functools import wraps
from fastapi import Request
from fastapi.responses import JSONResponse
from common.utils import get_logger

logger = get_logger(__name__)


class AuthManager:
    """Authentication manager using Supabase Auth"""

    def __init__(self, supabase_manager):
        self.supabase = supabase_manager
        self.supabase_url = os.environ.get("SUPABASE_URL", "")
        self.supabase_anon_key = os.environ.get("SUPABASE_ANON_KEY", "")

    async def get_current_user(self, request: Request) -> Optional[Dict[str, Any]]:
        try:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
                if token.startswith("ln-"):
                    user_id = self.supabase.validate_api_key(token)
                    if user_id:
                        return self.supabase.get_user(user_id)
                    return None
                user = await self._validate_supabase_token(token)
                if user:
                    return user
            session_token = request.cookies.get("llamanet_session")
            if session_token:
                user = await self._validate_supabase_token(session_token)
                if user:
                    return user
            return None
        except Exception as e:
            logger.debug(f"Auth error: {e}")
            return None

    async def _validate_supabase_token(self, token: str) -> Optional[Dict[str, Any]]:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {token}", "apikey": self.supabase_anon_key}
                async with session.get(
                    f"{self.supabase_url}/auth/v1/user", headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        user_id = data.get("id")
                        email = data.get("email", "")
                        if user_id:
                            user_meta = data.get("user_metadata", {})
                            return self.supabase.get_or_create_user(
                                user_id=user_id, email=email,
                                full_name=user_meta.get("full_name", ""),
                                avatar_url=user_meta.get("avatar_url", ""),
                                google_id=user_meta.get("provider_id", ""),
                            )
            return None
        except Exception as e:
            logger.debug(f"Token validation error: {e}")
            return None


def require_auth(auth_manager: AuthManager):
    def decorator(handler: Callable):
        @wraps(handler)
        async def wrapper(request: Request, *args, **kwargs):
            user = await auth_manager.get_current_user(request)
            if not user:
                return JSONResponse(status_code=401, content={"error": "Authentication required"})
            request.state.user = user
            return await handler(request, *args, **kwargs)
        return wrapper
    return decorator
