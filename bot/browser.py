"""Live browser mode — drive a real page with a real browser.

Why a browser and not just HTTP? Because most modern CAPTCHAs live in a
canvas widget and are validated against a behavioural trace the browser
produces. An HTTP-only client can fetch the image and compute the answer,
but it cannot make the page accept it — unless the endpoint trusts the
client (the `api_only` weakness the lab measures).

So this module drives Chromium via Playwright:

  1. open the page,
  2. locate the CAPTCHA image (auto-detected, or given by CSS selector),
  3. solve it with the matching solver,
  4. replay a human-like pointer trace to the answer position,
  5. click / type / submit,
  6. read back the outcome.

If Playwright is not installed, the module degrades gracefully and says
so instead of exploding.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .behavior import human_trace


@dataclass
class LiveResult:
    """Outcome of a live-browser attempt."""

    family: str
    answer: Any = None
    solved: bool = False
    confidence: float = 0.0
    duration_ms: float = 0.0
    trace: list[dict] = field(default_factory=list)
    page_text: str = ""
    screenshot: str | None = None
    error: str | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"family": self.family, "answer": self.answer,
             "solved": self.solved, "confidence": round(self.confidence, 4),
             "duration_ms": round(self.duration_ms, 1),
             "page_text": self.page_text[:500], "error": self.error,
             "meta": self.meta}
        if self.screenshot:
            d["screenshot"] = self.screenshot
        return d


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _element_screenshot(el) -> bytes:
    return el.screenshot()


def _b64_to_bytes(data_url: str) -> bytes:
    _, _, b64 = data_url.partition(",")
    return base64.b64decode(b64)


class BrowserSolver:
    """Solve a CAPTCHA embedded in a live page."""

    def __init__(self, headless: bool = True, timeout_ms: int = 20000,
                 viewport: tuple[int, int] = (1280, 900),
                 user_agent: str | None = None):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.viewport = viewport
        self.user_agent = user_agent

    # ------------------------------------------------------------------
    def _launch(self):
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=self.headless,
                                      args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": self.viewport[0],
                                            "height": self.viewport[1]},
                                  user_agent=self.user_agent,
                                  locale="es-ES")
        return pw, browser, ctx

    def solve(self, url: str, family: str = "slider",
              image_selector: str | None = None,
              piece_selector: str | None = None,
              input_selector: str | None = None,
              submit_selector: str | None = None,
              target: str | None = None,
              grid: int = 3, tile_size: int = 96, gap: int = 8,
              screenshot_out: str | None = None,
              **kwargs) -> LiveResult:
        """Drive the page and attempt to solve the CAPTCHA."""
        if not playwright_available():
            return LiveResult(family=family, error=(
                "playwright not installed — pip install playwright && "
                "playwright install chromium"))

        t0 = time.time()
        pw = browser = ctx = None
        try:
            pw, browser, ctx = self._launch()
            page = ctx.new_page()
            page.goto(url, timeout=self.timeout_ms)
            page.wait_for_timeout(600)

            img_el = self._find_image(page, image_selector)
            if img_el is None:
                return LiveResult(family=family, error=(
                    "no CAPTCHA image found (pass --image-selector)"),
                    duration_ms=round((time.time() - t0) * 1000, 1))

            img_bytes = _element_screenshot(img_el)
            box = img_el.bounding_box() or {"x": 0, "y": 0, "width": 0, "height": 0}

            from .solvers import get_solver
            solver = get_solver(family, grid=grid, tile_size=tile_size,
                                gap=gap, target=target)

            if family == "slider":
                piece_el = self._find_piece(page, piece_selector)
                if piece_el is None:
                    return LiveResult(family=family, error=(
                        "slider needs the piece sprite (pass --piece-selector)"),
                        duration_ms=round((time.time() - t0) * 1000, 1))
                piece_bytes = _element_screenshot(piece_el)
                res = solver.solve_pair(img_bytes, piece_bytes)
                answer = res.answer or {}
                trace = human_trace(
                    box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5,
                    box["x"] + min(answer.get("x", 0), box["width"] - 4),
                    box["y"] + box["height"] * 0.5)
                self._replay_mouse(page, trace)
                self._drag(page, trace)

            elif family == "text":
                res = solver.solve_image(img_bytes)
                answer = res.answer
                if input_selector:
                    page.fill(input_selector, answer or "")
                else:
                    page.keyboard.type(answer or "", delay=rng_delay())
                trace = []

            elif family == "grid":
                res = solver.solve_image(img_bytes, target=target)
                answer = res.answer or []
                trace = []
                for idx in answer:
                    r, c = divmod(int(idx), grid)
                    # Cell origin in page coordinates.
                    cw = box["width"] / grid
                    ch = box["height"] / grid
                    cx = box["x"] + c * cw + cw / 2
                    cy = box["y"] + r * ch + ch / 2
                    t = human_trace(cx - 80, cy - 60, cx, cy)
                    self._replay_mouse(page, t)
                    trace.extend(t)

            elif family == "image":
                res = solver.solve_image(img_bytes, target=target)
                answer = res.answer or {}
                if answer:
                    cx = box["x"] + answer["x"]
                    cy = box["y"] + answer["y"]
                    trace = human_trace(cx - 100, cy - 70, cx, cy)
                    self._replay_mouse(page, trace)

            else:
                return LiveResult(family=family,
                                  error=f"unsupported family {family!r}")

            if submit_selector:
                page.click(submit_selector, timeout=self.timeout_ms)
            page.wait_for_timeout(900)

            text = ""
            try:
                text = page.inner_text("body")[:2000]
            except Exception:
                pass

            shot = None
            if screenshot_out:
                page.screenshot(path=screenshot_out, full_page=True)
                shot = screenshot_out

            solved = self._judge(text, res)
            return LiveResult(
                family=family, answer=answer, solved=solved,
                confidence=res.confidence,
                duration_ms=round((time.time() - t0) * 1000, 1),
                trace=trace, page_text=text, screenshot=shot,
                meta={"solver_meta": res.meta})

        except Exception as e:
            return LiveResult(family=family, error=f"{type(e).__name__}: {e}",
                              duration_ms=round((time.time() - t0) * 1000, 1))
        finally:
            for closer in (ctx, browser):
                try:
                    closer and closer.close()
                except Exception:
                    pass
            try:
                pw and pw.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    @staticmethod
    def _find_image(page, selector: str | None):
        if selector:
            return page.query_selector(selector)
        for sel in ("img[src^='data:image']", "canvas", "img"):
            el = page.query_selector(sel)
            if el is not None:
                return el
        return None

    @staticmethod
    def _find_piece(page, selector: str | None):
        if selector:
            return page.query_selector(selector)
        # The piece sprite is the small floating image next to the board.
        cands = page.query_selector_all("img[src^='data:image']")
        if len(cands) >= 2:
            return cands[-1]
        return None

    @staticmethod
    def _replay_mouse(page, trace: list[dict]) -> None:
        """Move the mouse along the trace without pressing."""
        if not trace:
            return
        page.mouse.move(trace[0]["x"], trace[0]["y"])
        for p in trace[1:]:
            page.mouse.move(p["x"], p["y"])

    @staticmethod
    def _drag(page, trace: list[dict]) -> None:
        """Press, move along the trace, release."""
        if not trace:
            return
        page.mouse.move(trace[0]["x"], trace[0]["y"])
        page.mouse.down()
        for p in trace[1:]:
            page.mouse.move(p["x"], p["y"])
        page.mouse.up()

    @staticmethod
    def _judge(page_text: str, res) -> bool:
        """Heuristic success detection from the page text."""
        t = (page_text or "").lower()
        bad = ("incorrect", "wrong", "failed", "invalid captcha", "try again")
        good = ("success", "verified", "solved", "accepted", "correct")
        if any(b in t for b in bad):
            return False
        if any(g in t for g in good):
            return True
        return bool(res.answer) and res.confidence > 0.6


def rng_delay() -> int:
    """Typing delay in ms — random-ish but reproducible per call."""
    import random
    return random.randint(45, 130)
