"""Solver registry — one entry per challenge family.

Each solver wraps the lab's family class (which contains both the generator
and the attacker) and exposes a narrow, stable surface for the CLI and for
the live-browser mode:

    solve_image(img_bytes) -> SolverResult

The point of this module is to hide the lab internals. A caller who only
wants "read this CAPTCHA image and tell me the answer" should not have to
know that the text attacker is a family class with a `solve(Challenge)`
method, nor that the grid attacker needs the grid geometry.
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from app.families.base import Attempt, Challenge


@dataclass
class SolverResult:
    """Outcome of one solve attempt."""

    family: str
    answer: Any = None
    confidence: float = 0.0
    duration_ms: float = 0.0
    ok: bool = False
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "family": self.family,
            "answer": self.answer,
            "confidence": round(self.confidence, 4),
            "duration_ms": round(self.duration_ms, 1),
            "ok": self.ok,
            "meta": self.meta,
        }


class BaseSolver:
    """Common plumbing: build a challenge from a raw image, run the attacker."""

    name = "base"

    def _build_challenge(self, img: Image.Image, **public) -> Challenge:
        raise NotImplementedError

    def _attempt_to_result(self, attempt: Attempt) -> SolverResult:
        raise NotImplementedError

    def solve_image(self, image: bytes | Image.Image, **kwargs) -> SolverResult:
        """Solve a CAPTCHA given only the image bytes."""
        t0 = time.time()
        img = Image.open(io.BytesIO(image)).convert("RGB") if isinstance(
            image, (bytes, bytearray)) else image.convert("RGB")
        challenge = self._build_challenge(img, **kwargs)
        attempt = self._solve(challenge)
        result = self._attempt_to_result(attempt)
        result.duration_ms = result.duration_ms or round((time.time() - t0) * 1000, 1)
        return result

    def _solve(self, challenge: Challenge) -> Attempt:
        raise NotImplementedError


# --------------------------------------------------------------------------
# slider
# --------------------------------------------------------------------------

class SliderSolver(BaseSolver):
    """Drag-to-fit slider: locate the gap from the background + piece sprite.

    Two strategies, because real sites differ:

    * **template** — correlate the piece sprite against the board
      (masked NCC over FFT). Exact when the sprite carries the pristine
      texture of the gap.
    * **contrast** — find the gap as a dark region in the board, ignoring
      the sprite entirely. Needed whenever the site decorates the piece or
      repaints the gap's interior, which breaks the correlation.

    ``strategy="auto"`` runs the template match first and falls back to the
    contrast detector when the template's confidence is low — which is
    exactly what a real attacker has to do in the field.
    """

    name = "slider"

    def solve_pair(self, background: bytes | Image.Image,
                   piece: bytes | Image.Image,
                   strategy: str = "auto",
                   sprite: dict | None = None) -> SolverResult:
        """Solve a slider given the background image and the piece sprite.

        ``sprite`` is the optional geometry block a site exposes alongside
        the images (``{"opaque_x", "opaque_y", "opaque_w", "opaque_h",
        "origin_x", "origin_y"}``).

        Three strategies, because real sites differ — and the order
        matters:

        * **geometry** — the site's own sprite block gives the piece's
          origin on the canvas. When that origin is the *gap* (some sites
          cut the sprite straight out of the hole), the payload leaks the
          answer outright: no image analysis at all. This is the `api_only`
          weakness the lab measures, and it is why the client must not
          trust its own geometry.
        * **template** — correlate the piece sprite against the board
          (masked NCC over FFT). Exact when the sprite carries the pristine
          texture of the gap.
        * **contrast** — find the gap as a dark region in the board,
          ignoring the sprite entirely. Needed when the site decorates the
          piece or repaints the gap's interior, which breaks the
          correlation.

        ``strategy="auto"`` prefers geometry when the site offers it,
        falls back to the template match, and uses the contrast detector
        when the template's confidence is low.
        """
        from app.solver import solve_slider
        from bot.gapdetect import detect_gap

        bg = (Image.open(io.BytesIO(background)).convert("RGB")
              if isinstance(background, (bytes, bytearray))
              else background.convert("RGB"))
        pc = (Image.open(io.BytesIO(piece)).convert("RGBA")
              if isinstance(piece, (bytes, bytearray))
              else piece.convert("RGBA"))

        t0 = time.time()
        attempts: list[dict] = []

        # ---- strategy 1: geometry from the payload ----------------------
        if strategy in ("auto", "geometry") and sprite:
            geo = self._from_geometry(sprite)
            if geo:
                attempts.append(geo)

        if strategy in ("auto", "template"):
            res = solve_slider(bg, pc)
            if res.get("error"):
                attempts.append({"strategy": "template", "error": res["error"]})
            else:
                attempts.append({
                    "strategy": "template",
                    "x": res["best_x"], "y": res["best_y"],
                    "confidence": float(res.get("confidence", 0.0)),
                    "ncc": res.get("ncc"),
                })

        if strategy in ("auto", "contrast"):
            gap = detect_gap(bg)
            if gap.get("error"):
                attempts.append({"strategy": "contrast",
                                 "error": gap["error"]})
            else:
                attempts.append({
                    "strategy": "contrast",
                    "x": gap["x"], "y": gap["y"],
                    "confidence": float(gap["confidence"]),
                    "bbox": gap["bbox"],
                })

        if not attempts:
            return SolverResult(family=self.name, answer=None, ok=False,
                                duration_ms=round((time.time() - t0) * 1000, 1),
                                meta={"error": "no strategy produced a result"})

        # The template match reports the TOP-LEFT of the opaque shape; the
        # verify endpoint expects the CENTRE of that shape. The two are
        # different references, and mixing them is the classic source of a
        # constant pixel offset. Each strategy therefore reports its own
        # reference and the final answer is normalised to the centre.
        # ---- strategy selection -----------------------------------------
        geometry = next((a for a in attempts
                         if a["strategy"] == "geometry" and "x" in a), None)
        template = next((a for a in attempts
                         if a["strategy"] == "template" and "x" in a), None)
        contrast = next((a for a in attempts
                         if a["strategy"] == "contrast" and "x" in a), None)

        if strategy == "geometry" and geometry:
            chosen, ref = geometry, "centre"
        elif strategy == "template" and template:
            chosen, ref = template, "top-left"
        elif strategy == "contrast" and contrast:
            chosen, ref = contrast, "centre"
        elif geometry:
            # The payload already carries the answer.
            chosen, ref = geometry, "centre"
        elif template and contrast:
            # Prefer the template when it is confident; the contrast
            # detector is the fallback for decorated sprites.
            if template["confidence"] >= 0.75:
                chosen, ref = template, "top-left"
            else:
                chosen, ref = contrast, "centre"
        else:
            chosen = template or contrast
            ref = "top-left" if chosen is template else "centre"

        answer = {"x": chosen["x"], "y": chosen["y"]}
        if ref == "top-left":
            answer["reference"] = "shape top-left"
            answer["centre"] = None
        else:
            answer["reference"] = "gap centre"

        return SolverResult(
            family=self.name,
            answer=answer,
            confidence=chosen.get("confidence", 0.0),
            duration_ms=round((time.time() - t0) * 1000, 1),
            ok=True,
            meta={"strategy": chosen["strategy"], "attempts": attempts,
                  "reference": ref},
        )

    @staticmethod
    def _from_geometry(sprite: dict) -> dict | None:
        """Read the gap position straight out of the site's sprite block.

        Sites that cut the piece out of the hole publish the sprite's
        origin on the canvas — which *is* the gap. When that is the case,
        the answer needs no computer vision at all.
        """
        try:
            ox = int(sprite.get("opaque_x", 0))
            oy = int(sprite.get("opaque_y", 0))
            ow = int(sprite["opaque_w"])
            oh = int(sprite["opaque_h"])
            origin_x = int(sprite["origin_x"])
            origin_y = int(sprite["origin_y"])
        except (KeyError, TypeError, ValueError):
            return None
        return {
            "strategy": "geometry",
            "x": origin_x + ox + ow // 2,
            "y": origin_y + oy + oh // 2,
            "confidence": 1.0,
            "note": "position read from the client payload — the endpoint "
                    "leaks the answer, no image analysis required",
        }


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------

class TextSolver(BaseSolver):
    """Text CAPTCHA: read the characters with local OCR."""

    name = "text"

    def _build_challenge(self, img: Image.Image, **public) -> Challenge:
        from app.families.text_family import ALPHABET, TextFamily
        return Challenge(
            family="text",
            cid=Challenge.new_id(),
            seed=0,
            created=time.time(),
            public={"family": "text", "image": _data_url(img),
                    "charset": ALPHABET},
            truth={},
        )

    def _solve(self, challenge: Challenge) -> Attempt:
        from app.families.text_family import TextFamily
        return TextFamily().solve(challenge)

    def _attempt_to_result(self, attempt: Attempt) -> SolverResult:
        answer = attempt.answer or ""
        # Confidence heuristic: OCR gives no calibrated score, so use the
        # fraction of characters that survived charset filtering, tempered
        # by how long the string is (a 1-char read is far less reliable
        # than a 6-char one).
        meta = attempt.meta or {}
        hits = meta.get("charset_hits", 0)
        n = max(1, len(answer))
        conf = min(1.0, (hits / n)) * min(1.0, n / 4.0)
        return SolverResult(
            family=self.name,
            answer=answer,
            confidence=conf,
            duration_ms=attempt.duration_ms,
            ok=bool(answer),
            meta=meta,
        )


# --------------------------------------------------------------------------
# grid
# --------------------------------------------------------------------------

class GridSolver(BaseSolver):
    """Grid selection CAPTCHA: pick every cell matching the target class."""

    name = "grid"

    def __init__(self, grid: int = 3, tile_size: int = 96, gap: int = 8,
                 target: str | None = None):
        self.grid = grid
        self.tile_size = tile_size
        self.gap = gap
        self.target = target

    def _build_challenge(self, img: Image.Image, **public) -> Challenge:
        from app.families.grid_family import GridFamily
        target = public.get("target") or self.target
        if target is None:
            target = self._infer_target(img)
        return Challenge(
            family="grid",
            cid=Challenge.new_id(),
            seed=0,
            created=time.time(),
            public={"family": "grid", "image": _data_url(img),
                    "grid": self.grid, "tile_size": self.tile_size,
                    "gap": self.gap, "target": target},
            truth={},
        )

    def _infer_target(self, img: Image.Image) -> str:
        """Guess the target class when the caller does not tell us.

        A real attacker often does not know what to look for either. The
        heuristic: the target class is usually the one with the most
        instances (CAPTCHA designers pick a class that appears a few
        times, not once). We classify every cell and pick the majority
        class.
        """
        from app.families.grid_family import GridFamily
        counts: dict[str, int] = {}
        for i in range(self.grid * self.grid):
            r, c = divmod(i, self.grid)
            x0 = c * (self.tile_size + self.gap)
            y0 = r * (self.tile_size + self.gap)
            tile = img.crop((x0, y0, x0 + self.tile_size, y0 + self.tile_size))
            label, _ = GridFamily._classify(tile)
            counts[label] = counts.get(label, 0) + 1
        return max(counts, key=counts.get) if counts else "bus"

    def _solve(self, challenge: Challenge) -> Attempt:
        from app.families.grid_family import GridFamily
        return GridFamily().solve(challenge)

    def _attempt_to_result(self, attempt: Attempt) -> SolverResult:
        meta = attempt.meta or {}
        scored = meta.get("cells_scored", {})
        target = meta.get("target")
        picked = list(attempt.answer or [])
        # Confidence: mean score of the cells we picked.
        vals = [scored.get(str(i), (None, 0.0))[1] for i in picked]
        vals = [v for v in vals if isinstance(v, (int, float))]
        conf = (sum(vals) / len(vals)) if vals else 0.0
        return SolverResult(
            family=self.name,
            answer=picked,
            confidence=conf,
            duration_ms=attempt.duration_ms,
            ok=bool(picked),
            meta=meta,
        )


# --------------------------------------------------------------------------
# image (single click)
# --------------------------------------------------------------------------

class ImageSolver(BaseSolver):
    """Single-image CAPTCHA: click the object matching the target."""

    name = "image"

    def __init__(self, target: str | None = None):
        self.target = target

    def _build_challenge(self, img: Image.Image, **public) -> Challenge:
        from app.families.image_family import ImageFamily
        target = public.get("target") or self._infer_target(img)
        return Challenge(
            family="image",
            cid=Challenge.new_id(),
            seed=0,
            created=time.time(),
            public={"family": "image", "image": _data_url(img),
                    "target": target},
            truth={},
        )

    def _infer_target(self, img: Image.Image) -> str:
        from app.families.grid_family import GridFamily
        from app.families.image_family import ImageFamily
        boxes = ImageFamily._detect_blobs(img)
        if not boxes:
            return "bus"
        counts: dict[str, int] = {}
        for box in boxes:
            crop = img.crop((box["x"], box["y"],
                             box["x"] + box["w"], box["y"] + box["h"]))
            label, _ = GridFamily._classify(crop)
            counts[label] = counts.get(label, 0) + 1
        return max(counts, key=counts.get)

    def _solve(self, challenge: Challenge) -> Attempt:
        from app.families.image_family import ImageFamily
        return ImageFamily().solve(challenge)

    def _attempt_to_result(self, attempt: Attempt) -> SolverResult:
        meta = attempt.meta or {}
        box = attempt.answer
        # The answer is a bounding box; an attacker clicks its centre.
        answer = None
        if box:
            answer = {"x": box["x"] + box["w"] // 2,
                      "y": box["y"] + box["h"] // 2,
                      "box": box}
        return SolverResult(
            family=self.name,
            answer=answer,
            confidence=float(meta.get("confidence", 0.0)),
            duration_ms=attempt.duration_ms,
            ok=bool(box),
            meta=meta,
        )


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

def _data_url(img: Image.Image) -> str:
    from app.families.base import to_data_url
    return to_data_url(img)


def get_solver(family: str, **kwargs) -> BaseSolver:
    """Instantiate a solver by family name."""
    family = family.lower().strip()
    if family == "slider":
        return SliderSolver()
    if family in ("text", "ocr"):
        return TextSolver()
    if family == "grid":
        return GridSolver(**kwargs)
    if family in ("image", "click"):
        return ImageSolver(target=kwargs.get("target"))
    raise ValueError(f"unknown family: {family!r} "
                     f"(known: slider, text, grid, image)")


SOLVERS = ("slider", "text", "grid", "image")
