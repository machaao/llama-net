import ipaddress
from typing import Dict, Any, Optional, Tuple
from urllib.parse import urlparse
from common.utils import get_logger

logger = get_logger(__name__)

MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024      # 1 MB
MAX_MESSAGE_COUNT = 50
MAX_MESSAGE_CHARS = 100_000
MAX_TOTAL_CONTEXT_CHARS = 200_000
MAX_PROMPT_CHARS = 200_000
MAX_MAX_TOKENS = 16384
MIN_MAX_TOKENS = 1

BLOCKED_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "169.254.170.2",
    "fd00:ec2::254",
}

BLOCKED_CIDRS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class ValidationError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: Dict = None):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


class RequestValidator:

    @staticmethod
    def validate_chat_request(body: Dict[str, Any]) -> None:
        messages = body.get("messages", [])
        if not messages:
            raise ValidationError("Messages array cannot be empty.", 400)
        if len(messages) > MAX_MESSAGE_COUNT:
            raise ValidationError(
                f"Too many messages. Maximum {MAX_MESSAGE_COUNT} allowed, got {len(messages)}.", 400
            )

        total_chars = 0
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                raise ValidationError(f"Message {i} must be an object.", 400)
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role not in ("system", "user", "assistant", "function", "tool"):
                raise ValidationError(
                    f"Message {i} has invalid role '{role}'. Must be one of: system, user, assistant, function, tool.", 400
                )
            if not isinstance(content, str):
                if role == "assistant" and content is None:
                    content = ""
                else:
                    raise ValidationError(f"Message {i} content must be a string.", 400)
            if len(content) > MAX_MESSAGE_CHARS:
                raise ValidationError(
                    f"Message {i} exceeds maximum length of {MAX_MESSAGE_CHARS:,} characters (got {len(content):,}).", 400
                )
            total_chars += len(content)

        if total_chars > MAX_TOTAL_CONTEXT_CHARS:
            raise ValidationError(
                f"Total message content exceeds {MAX_TOTAL_CONTEXT_CHARS:,} characters (got {total_chars:,}).", 400
            )

        max_tokens = body.get("max_tokens")
        if max_tokens is not None:
            if not isinstance(max_tokens, int) or max_tokens < MIN_MAX_TOKENS:
                raise ValidationError(f"max_tokens must be an integer >= {MIN_MAX_TOKENS}.", 400)
            if max_tokens > MAX_MAX_TOKENS:
                raise ValidationError(f"max_tokens exceeds maximum of {MAX_MAX_TOKENS}. Requested: {max_tokens}.", 400)

        temperature = body.get("temperature")
        if temperature is not None:
            if not isinstance(temperature, (int, float)):
                raise ValidationError("temperature must be a number.", 400)
            if temperature < 0 or temperature > 2.0:
                raise ValidationError("temperature must be between 0 and 2.0.", 400)

        top_p = body.get("top_p")
        if top_p is not None:
            if not isinstance(top_p, (int, float)):
                raise ValidationError("top_p must be a number.", 400)
            if top_p < 0 or top_p > 1.0:
                raise ValidationError("top_p must be between 0 and 1.0.", 400)

    @staticmethod
    def validate_completion_request(body: Dict[str, Any]) -> None:
        prompt = body.get("prompt", "")
        if isinstance(prompt, list):
            if len(prompt) == 0:
                raise ValidationError("Prompt array cannot be empty.", 400)
            if len(prompt) > 1:
                raise ValidationError("Only a single prompt is supported.", 400)
            prompt = prompt[0]
        if not isinstance(prompt, str):
            raise ValidationError("Prompt must be a string.", 400)
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValidationError(f"Prompt exceeds maximum of {MAX_PROMPT_CHARS:,} characters (got {len(prompt):,}).", 400)

        max_tokens = body.get("max_tokens")
        if max_tokens is not None:
            if not isinstance(max_tokens, int) or max_tokens < MIN_MAX_TOKENS:
                raise ValidationError(f"max_tokens must be an integer >= {MIN_MAX_TOKENS}.", 400)
            if max_tokens > MAX_MAX_TOKENS:
                raise ValidationError(f"max_tokens exceeds maximum of {MAX_MAX_TOKENS}. Requested: {max_tokens}.", 400)

    @staticmethod
    def validate_request_body_size(content_length: Optional[int]) -> None:
        if content_length is not None and content_length > MAX_REQUEST_BODY_BYTES:
            raise ValidationError(
                f"Request body too large. Maximum {MAX_REQUEST_BODY_BYTES // 1024} KB allowed.", 413
            )

    @staticmethod
    def validate_node_url(url: str) -> Tuple[bool, str]:
        if not url:
            return True, ""
        try:
            parsed = urlparse(url)
        except Exception:
            return False, "Invalid URL format"
        if parsed.scheme not in ("https", "http"):
            return False, f"Invalid URL scheme: {parsed.scheme}"
        hostname = parsed.hostname
        if not hostname:
            return False, "No hostname in URL"
        if hostname.lower() in BLOCKED_HOSTS:
            return False, f"Blocked hostname: {hostname}"
        try:
            addr = ipaddress.ip_address(hostname)
            for cidr in BLOCKED_CIDRS:
                if addr in cidr:
                    return False, f"URL resolves to private IP range: {cidr}"
        except ValueError:
            safe_suffixes = (
                ".trycloudflare.com",
                ".cfargotunnel.com",
                ".machaao.com",
                ".llamanet.app",
            )
            if not any(hostname.endswith(s) for s in safe_suffixes):
                logger.debug(f"Node URL from unknown domain: {hostname}")
        return True, ""

    @staticmethod
    def validate_node_registration(body: Dict[str, Any]) -> None:
        node_id = body.get("node_id", "")
        if not node_id or not isinstance(node_id, str):
            raise ValidationError("node_id is required and must be a string.", 400)
        if len(node_id) > 200:
            raise ValidationError("node_id too long.", 400)
        model = body.get("model", "unknown")
        if isinstance(model, str) and len(model) > 500:
            raise ValidationError("Model name too long.", 400)
        url = body.get("url", "") or body.get("tunnel_url", "")
        if url:
            is_safe, reason = RequestValidator.validate_node_url(url)
            if not is_safe:
                raise ValidationError(f"Node URL rejected: {reason}", 400)
        port = body.get("port", 8000)
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise ValidationError("Invalid port number.", 400)
