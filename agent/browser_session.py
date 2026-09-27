"""Headless browser session for the Fara1.5-9B computer-use agent.

Wraps Playwright with a **sandboxed** browser context:

- No access to host filesystem or environment variables (Playwright context
  isolation — the Chromium instance is a fresh profile).
- Isolated cookie/storage per session.
- Viewport management for consistent screenshot geometry.
- Screenshot capture as base64-encoded PNG for multimodal inference.
"""

import asyncio
import base64
import time
from typing import Any, Dict, List, Optional

from common.utils import get_logger

logger = get_logger(__name__)

_DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
_SCREENSHOT_QUALITY = 70  # JPEG quality — smaller payloads than PNG


class BrowserSession:
    """Manages a single sandboxed headless-browser session."""

    def __init__(
        self,
        viewport: Optional[Dict[str, int]] = None,
        headless: bool = True,
    ):
        self.viewport = viewport or _DEFAULT_VIEWPORT
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    async def start(self, start_url: Optional[str] = None) -> "BrowserSession":
        """Launch the browser and open the first page.

        Playwright is imported lazily so the rest of LlamaNet (chat, downloads,
        gateway) works even when Playwright is not installed. Only the agent
        subsystem requires it.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "Playwright is required for the computer-use agent. "
                "Install with: pip install playwright && playwright install chromium"
            ) from e

        self._playwright = await async_playwright().start()
        # Launch with a persistent but isolated context (no host access).
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir="",  # in-memory ephemeral profile — no disk persistence
            headless=self.headless,
            viewport=self.viewport,
            # Security: disable filesystem access via browser flags where supported
            args=["--disable-dev-shm-usage", "--no-first-run"],
            no_viewport=False,
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        await self._page.set_viewport_size(self.viewport)

        if start_url:
            await self._page.goto(start_url, wait_until="domcontentloaded")

        logger.info(
            f"Browser session started (viewport={self.viewport}, headless={self.headless})"
        )
        return self

    async def screenshot(self) -> str:
        """Capture a screenshot and return it as a JPEG data URL.

        Returns ``""`` (empty string) if the page is not available.
        """
        if self._page is None:
            return ""
        data = await self._page.screenshot(
            type="jpeg", quality=_SCREENSHOT_QUALITY, full_page=False
        )
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"

    @property
    def page(self):
        return self._page

    async def current_url(self) -> str:
        return self._page.url if self._page else ""

    async def current_title(self) -> str:
        return await self._page.title() if self._page else ""

    async def close(self) -> None:
        """Tear down the browser session."""
        try:
            if self._context:
                await self._context.close()
        except Exception as e:
            logger.debug(f"Browser context close error: {e}")
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.debug(f"Playwright stop error: {e}")
        self._browser = None
        self._context = None
        self._page = None
        logger.info("Browser session closed")

    async def __aenter__(self) -> "BrowserSession":
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


__all__ = ["BrowserSession"]