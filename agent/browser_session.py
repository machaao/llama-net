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
_SCREENSHOT_QUALITY = 75  # JPEG quality — sharp enough for clicks, ~30% smaller payload per step

# ── Shared Chromium singleton ──
# One Chromium process is reused across agent sessions. Launching a browser
# per session costs seconds and ~200-400 MB of RSS churn each run. Per-session
# *contexts* remain isolated (fresh cookies/storage), so sandboxing is intact.
_shared_playwright = None
_shared_browser = None
_shared_browser_headless: Optional[bool] = None


async def _get_shared_playwright():
    global _shared_playwright
    if _shared_playwright is None:
        from playwright.async_api import async_playwright
        _shared_playwright = await async_playwright().start()
    return _shared_playwright


async def _get_shared_browser(playwright, headless: bool):
    """Return the shared Chromium, launching it only if absent or mode-mismatched."""
    global _shared_browser, _shared_browser_headless
    if _shared_browser is not None and _shared_browser_headless != headless:
        # Headless mode is fixed at launch — close and relaunch if it changed.
        try:
            await _shared_browser.close()
        except Exception:
            pass
        _shared_browser = None
    if _shared_browser is None:
        _shared_browser = await playwright.chromium.launch(
            headless=headless,
            args=["--disable-dev-shm-usage", "--no-first-run"],
        )
        _shared_browser_headless = headless
    return _shared_browser


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


# JS: interactive elements near a viewport point (for click-miss feedback).
_NEARBY_ELEMENTS_JS = """([x, y, radius]) => {
    const TAGS = new Set(['a','button','input','select','textarea','summary','label']);
    const ROLES = new Set(['button','link','tab','menuitem','checkbox','radio','switch','option']);
    const interactiveAncestor = (node) => {
        for (let i = 0; node && node.nodeType === 1 && i < 8; i++) {
            const t = node.tagName.toLowerCase();
            if (TAGS.has(t) || node.hasAttribute('href') || node.hasAttribute('onclick') ||
                ROLES.has(node.getAttribute('role') || '')) {
                if (node.disabled === true || node.getAttribute('aria-disabled') === 'true') return null;
                return node;
            }
            if (t === 'body' || t === 'html') return null;
            node = node.parentElement;
        }
        return null;
    };
    const box = {left: x - radius, top: y - radius, right: x + radius, bottom: y + radius};
    const out = [];
    const seen = new Set();
    for (const c of document.querySelectorAll('a, button, input, select, textarea, summary, label, [role], [onclick]')) {
        const target = interactiveAncestor(c);
        if (!target || seen.has(target)) continue;
        seen.add(target);
        const r = target.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        if (r.right < box.left || r.left > box.right || r.bottom < box.top || r.top > box.bottom) continue;
        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        out.push({
            tag: target.tagName.toLowerCase(),
            text: (target.textContent || '').trim().slice(0, 40),
            href: target.href || '',
            rect: {x: Math.round(r.left), y: Math.round(r.top),
                   w: Math.round(r.width), h: Math.round(r.height)},
            d: Math.hypot(cx - x, cy - y),
        });
    }
    out.sort((a, b) => a.d - b.d);
    return out.slice(0, 8);
}"""


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

        # Reuse the shared Chromium across sessions; each session gets its own
        # isolated context (fresh cookies/storage — no host access).
        self._playwright = await _get_shared_playwright()
        self._browser = await _get_shared_browser(self._playwright, self.headless)
        self._context = await self._browser.new_context(
            viewport=self.viewport,
            # Pin the capture space: screenshot px ≡ CSS px ≡ click px.
            # Without this, a non-1 device_scale_factor (Retina emulation)
            # doubles the screenshot while page.mouse.click() still takes
            # CSS px — every click lands off-target.
            device_scale_factor=1,
        )
        self._page = await self._context.new_page()

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
                    // Walk ancestors: a click bubbles up, so a <span> inside
                    // an <a href> IS interactive. Disabled controls are NOT —
                    // they swallow clicks without effect.
                    const TAGS = new Set(['a','button','input','select','textarea','summary','label']);
                    const ROLES = new Set(['button','link','tab','menuitem','checkbox','radio','switch','option']);
                    let interactive = false;
                    let node = el;
                    for (let i = 0; node && node.nodeType === 1 && i < 8; i++) {
                        const t = node.tagName.toLowerCase();
                        if (TAGS.has(t) || node.hasAttribute('href') || node.hasAttribute('onclick') ||
                            ROLES.has(node.getAttribute('role') || '')) {
                            interactive = !(node.disabled === true ||
                                node.getAttribute('aria-disabled') === 'true');
                            break;
                        }
                        if (t === 'body' || t === 'html') break;
                        node = node.parentElement;
                    }
                    const r = el.getBoundingClientRect();
                    return {
                        tag: el.tagName.toLowerCase(),
                        id: el.id || '',
                        cls: cls.slice(0, 80),
                        text: (el.textContent || '').trim().slice(0, 60),
                        href: el.href || '',
                        interactive,
                        rect: {
                            x: Math.round(r.left), y: Math.round(r.top),
                            w: Math.round(r.width), h: Math.round(r.height),
                        },
                    };
                }""",
                [x, y],
            )
        except Exception as e:
            logger.debug(f"elementFromPoint({x}, {y}) failed: {e}")
            return None

    async def interactive_elements_near(
        self, x: int, y: int, radius: int = 150,
    ) -> List[Dict[str, Any]]:
        """Interactive elements within ``radius`` px of (x, y), nearest first.

        Grounds click-miss feedback: the model learns what it actually hit and
        which clickable elements are nearby (with viewport-pixel boxes), so it
        can click the intended element's listed coordinates instead of
        re-guessing from the screenshot (whose coordinate space may differ
        from the viewport after llama.cpp's vision preprocessing).
        """
        if self._page is None:
            return []
        try:
            return await self._page.evaluate(_NEARBY_ELEMENTS_JS, [x, y, radius])
        except Exception as e:
            logger.debug(f"interactive_elements_near({x}, {y}) failed: {e}")
            return []

    async def click_at(
        self,
        x: int,
        y: int,
        button: str = "left",
        click_count: int = 1,
    ) -> Dict[str, Any]:
        """Click at (x, y) using Playwright's native element click.

        Resolves the coordinates to the DOM element at that point, then clicks
        through the element handle at the exact position. This engages
        Playwright's actionability + hit-target verification: if an overlay
        intercepts the pointer event, a TimeoutError is raised instead of the
        click silently landing on the overlay.

        Returns:
            {"clicked": bool, "element": dict|None, "intercepted": bool,
             "out_of_bounds": bool, "error": str|None}
        """
        if self._page is None:
            return {"clicked": False, "element": None, "intercepted": False,
                    "out_of_bounds": False, "error": "no page"}

        # ── Bounds check ──
        # The screenshot captures ONLY the viewport (full_page=False), so click
        # coordinates MUST be viewport pixels. An out-of-viewport point (e.g. a
        # footer link at y=983 in a 1280x800 viewport) has no
        # elementFromPoint hit and page.mouse.click() would silently no-op —
        # reject explicitly so the loop can tell the model to scroll first.
        vp_w = int(self.viewport.get("width", 0) or 0)
        vp_h = int(self.viewport.get("height", 0) or 0)
        if vp_w and vp_h and not (0 <= x < vp_w and 0 <= y < vp_h):
            return {
                "clicked": False, "element": None, "intercepted": False,
                "out_of_bounds": True,
                "error": f"coordinates ({x}, {y}) outside viewport {vp_w}x{vp_h}",
            }

        # Snapshot open pages so a target="_blank" click can be detected and
        # adopted after the click (see _adopt_new_tab).
        try:
            pages_before = list(self._context.pages)
        except Exception:
            pages_before = []

        element_info = await self.element_at(x, y)

        # No element at point (elementFromPoint is null for detached or fully
        # transparent targets) — a raw mouse click would silently no-op.
        # Fail closed so the loop reports the miss instead of pretending.
        if element_info is None:
            return {
                "clicked": False, "element": None, "intercepted": False,
                "out_of_bounds": False,
                "error": f"no element at point ({x}, {y})",
            }

        # Resolve to an ElementHandle for native click with hit-target checks.
        handle = await self._page.evaluate_handle(
            "([x, y]) => document.elementFromPoint(x, y)", [x, y]
        )
        element = handle.as_element()

        if element is None:
            try:
                await self._page.mouse.click(x, y, button=button,
                                             click_count=click_count)
                new_tab = self._adopt_new_tab(pages_before)
                return {"clicked": True, "element": element_info,
                        "intercepted": False, "error": None, "new_tab": new_tab}
            except Exception as e:
                return {"clicked": False, "element": element_info,
                        "intercepted": False, "error": str(e)}
            finally:
                await handle.dispose()

        try:
            box = await element.bounding_box()
            if box and element_info.get("tag") == "a" and element_info.get("href"):
                # Links: click the element CENTER, not the exact pixel. Model
                # inference takes seconds and the page can shift in between
                # (fonts, lazy content, sticky headers) — an exact-pixel
                # offset computed from a stale box lands on a neighbor. The
                # model already resolved this element via elementFromPoint,
                # so its center IS the intended target.
                await element.click(
                    button=button, click_count=click_count, timeout=3000,
                )
            elif box:
                # Click at the EXACT (x, y) via position offset — preserves
                # the model's intended target while engaging hit-target
                # verification (Playwright checks elementFromPoint at the
                # final click point matches this element or a descendant).
                position = {"x": max(0.0, x - box["x"]),
                            "y": max(0.0, y - box["y"])}
                await element.click(
                    button=button, click_count=click_count,
                    position=position, timeout=3000,
                )
            else:
                # No bounding box (detached/hidden) — raw click fallback.
                await self._page.mouse.click(x, y, button=button,
                                             click_count=click_count)
            new_tab = self._adopt_new_tab(pages_before)
            return {"clicked": True, "element": element_info,
                    "intercepted": False, "error": None, "new_tab": new_tab}
        except Exception as e:
            err = str(e)
            intercepted = "intercept" in err.lower()
            logger.warning(
                f"🖱 Click at ({x}, {y}) on <{element_info.get('tag')}> "
                f"{'intercepted' if intercepted else 'failed'}: {err}"
            )
            return {"clicked": False, "element": element_info,
                    "intercepted": intercepted, "error": err}
        finally:
            await handle.dispose()

    def _adopt_new_tab(self, pages_before: list) -> bool:
        """Switch to a page opened by the last action (target="_blank" links).

        A new-tab click leaves the current page's URL/scroll unchanged, which
        the loop would misread as "click had no effect". Adopting the newest
        page keeps the next screenshot on what the user actually opened.
        Returns True when a new page was adopted.
        """
        try:
            opened = [p for p in self._context.pages if p not in pages_before]
            if opened:
                self._page = opened[-1]
                logger.info(f"🔗 New tab opened — adopted: {self._page.url}")
                return True
        except Exception as e:
            logger.debug(f"New-tab check failed: {e}")
        return False

    async def _scroll_state(self) -> Dict[str, Any]:
        """Combined scroll position state in a single evaluate."""
        return await self._page.evaluate(
            """() => {
                const el = document.scrollingElement || document.documentElement;
                const y = window.scrollY || 0;
                const x = window.scrollX || 0;
                const max = Math.max(0, el.scrollHeight - el.clientHeight);
                return {
                    x, y, max,
                    at_bottom: y >= max - 2,
                    at_top: y <= 2,
                    can_scroll: max > 10,
                };
            }"""
        )

    async def current_scroll(self) -> Dict[str, Any]:
        """Authoritative scroll position for prompt grounding.

        Returns ``{"x", "y", "max_y", "at_bottom", "at_top", "can_scroll"}``.
        Empty defaults when no page is open or the evaluate fails.
        """
        empty = {"x": 0, "y": 0, "max_y": 0, "at_bottom": False,
                 "at_top": True, "can_scroll": False}
        if self._page is None:
            return dict(empty)
        try:
            s = await self._scroll_state()
        except Exception as e:
            logger.debug(f"current_scroll failed: {e}")
            return dict(empty)
        return {"x": s["x"], "y": s["y"], "max_y": s["max"],
                "at_bottom": s["at_bottom"], "at_top": s["at_top"],
                "can_scroll": s["can_scroll"]}

    async def scroll_by(self, delta_x: int, delta_y: int) -> Dict[str, Any]:
        """Scroll by (delta_x, delta_y) pixels and verify the page moved.

        Parks the mouse at the viewport center before wheeling — ``mouse.wheel``
        dispatches at the current pointer position, and a pointer resting over
        a fixed navbar or overlay can swallow the wheel event.

        Returns:
            {"scrolled": bool, "scroll_x": int, "scroll_y": int,
             "can_scroll": bool, "delta_x": int, "delta_y": int}
        """
        if self._page is None:
            return {"scrolled": False, "scroll_x": 0, "scroll_y": 0,
                    "scroll_y_before": 0, "max_scroll_y": 0,
                    "at_bottom": False, "at_top": True,
                    "can_scroll": False, "delta_x": delta_x, "delta_y": delta_y}

        vp = self.viewport
        # Park pointer at viewport center so the wheel targets main content.
        await self._page.mouse.move(vp["width"] // 2, vp["height"] // 2)

        before = await self._scroll_state()

        await self._page.mouse.wheel(delta_x, delta_y)

        # Smooth-scroll animations apply over time — poll briefly.
        after = before
        for _ in range(10):
            await asyncio.sleep(0.05)
            after = await self._scroll_state()
            if after["x"] != before["x"] or after["y"] != before["y"]:
                break

        scrolled = after["x"] != before["x"] or after["y"] != before["y"]
        return {
            "scrolled": scrolled,
            "scroll_x": after["x"],
            "scroll_y": after["y"],
            "scroll_y_before": before["y"],
            "max_scroll_y": after["max"],
            "at_bottom": after["at_bottom"],
            "at_top": after["at_top"],
            "can_scroll": before["can_scroll"],
            "delta_x": delta_x,
            "delta_y": delta_y,
        }

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
        # Intentionally do NOT stop the shared Playwright/Chromium — the next
        # session reuses them (see _get_shared_browser).
        self._browser = None
        self._context = None
        self._page = None
        logger.info("Browser session closed")

    async def __aenter__(self) -> "BrowserSession":
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


__all__ = ["BrowserSession"]