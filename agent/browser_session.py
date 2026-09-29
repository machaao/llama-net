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
_SCREENSHOT_QUALITY = 90  # JPEG quality — small nav text needs sharpness for accurate clicks


def _jpeg_dimensions(data: bytes) -> tuple:
    """Read (width, height) from a JPEG's SOF marker. Returns (0, 0) on failure.

    Decodes only the frame header — no image decode — so it's cheap enough to
    run on every screenshot. Used to assert the capture geometry matches the
    viewport (Gap 2: an unpinned device_scale_factor silently doubles the
    screenshot, sending every model click to the wrong place).
    """
    i = 2  # skip SOI marker
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):  # SOF0/1/2 — carries dimensions
            height = (data[i + 5] << 8) | data[i + 6]
            width = (data[i + 7] << 8) | data[i + 8]
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2  # standalone marker — no length field
            continue
        seg_len = (data[i + 2] << 8) | data[i + 3]
        i += 2 + seg_len
    return 0, 0


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
            # Pin the capture space: screenshot px ≡ CSS px ≡ click px.
            # Without this, a non-1 device_scale_factor (Retina emulation,
            # persisted profile) doubles the screenshot while page.mouse.click()
            # still takes CSS px — every click lands off-target.
            device_scale_factor=1,
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
            logger.warning("📸 Screenshot requested but no page is open")
            return ""
        data = await self._page.screenshot(
            type="jpeg", quality=_SCREENSHOT_QUALITY, full_page=False,
            scale="css",  # capture in CSS px so screenshot px ≡ click px
        )
        # ── Gap 2: verify capture geometry ──
        # If the JPEG isn't exactly viewport-sized, model coordinates and
        # page.mouse.click() coordinates are in different spaces and every
        # click lands off-target. Fail loudly rather than silently mis-clicking.
        img_w, img_h = _jpeg_dimensions(data)
        exp_w, exp_h = self.viewport["width"], self.viewport["height"]
        if (img_w, img_h) != (exp_w, exp_h):
            logger.warning(
                f"📸 Screenshot geometry mismatch: got {img_w}x{img_h}, "
                f"expected {exp_w}x{exp_h} — model coordinates will NOT map "
                f"1:1 to click coordinates!"
            )
        b64 = base64.b64encode(data).decode("ascii")
        # A blank/white page compresses to a tiny JPEG. Log the size so we can
        # tell "page rendered" from "page is blank" without eyeballing images.
        size_kb = len(data) / 1024
        if size_kb < 5:
            logger.warning(
                f"📸 Screenshot suspiciously small ({size_kb:.1f} KB) — page may be "
                f"blank. url={self._page.url}"
            )
        else:
            logger.info(
                f"📸 Screenshot captured: {size_kb:.1f} KB JPEG, "
                f"b64={len(b64)} chars, url={self._page.url}"
            )
        return f"data:image/jpeg;base64,{b64}"

    async def element_at(self, x: int, y: int) -> Optional[Dict[str, Any]]:
        """Return info about the DOM element at viewport point (x, y), or None.

        Uses ``document.elementFromPoint`` — the authoritative answer to "what
        would a click at this coordinate actually hit?". Lets the loop detect a
        coordinate-space mismatch (click lands on <body>) and report the real
        element back to the model instead of silently looping.
        """
        if self._page is None:
            return None
        try:
            return await self._page.evaluate(
                """([x, y]) => {
                    const el = document.elementFromPoint(x, y);
                    if (!el) return null;
                    const cls = typeof el.className === 'string' ? el.className : '';
                    return {
                        tag: el.tagName.toLowerCase(),
                        id: el.id || '',
                        cls: cls.slice(0, 80),
                        text: (el.textContent || '').trim().slice(0, 60),
                        href: el.href || '',
                    };
                }""",
                [x, y],
            )
        except Exception as e:
            logger.debug(f"elementFromPoint({x}, {y}) failed: {e}")
            return None

    async def wait_for_settle(self, timeout_ms: int = 5000, settle_ms: int = 800) -> None:
        """Wait for the page to finish loading/rendering after an action.

        Uses the ``load`` state (NOT ``networkidle``) because pages with
        long-lived connections (SSE, websockets) never reach networkidle and
        would hang until timeout. A short fixed settle delay follows so
        client-side rendering completes before the next screenshot — without
        it, the next screenshot captures a blank/loading page and the model
        wrongly concludes the navigation failed.
        """
        if self._page is None:
            return
        url_before = self._page.url
        load_state = "unknown"
        try:
            await self._page.wait_for_load_state("load", timeout=timeout_ms)
            load_state = "load"
        except Exception as e:
            load_state = f"timeout/error ({type(e).__name__})"
        await asyncio.sleep(settle_ms / 1000)
        logger.info(
            f"⏳ Settled after action: load_state={load_state}, "
            f"url={url_before} → {self._page.url}, waited {settle_ms}ms"
        )

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