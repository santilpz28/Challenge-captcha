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

from core.contract import Attempt, Challenge


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
        from bot.slider_solver import solve_slider
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
        from bot.text_solver import DEFAULT_ALPHABET
        return Challenge(
            family="text",
            cid=Challenge.new_id(),
            seed=0,
            created=time.time(),
            public={"family": "text", "image": _data_url(img),
                    "charset": public.get("charset", DEFAULT_ALPHABET)},
            truth={},
        )

    def _solve(self, challenge: Challenge) -> Attempt:
        from bot.text_solver import solve_text
        return solve_text(challenge)

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
        from bot.grid_solver import cell_boxes, _classify_classical
        counts: dict[str, int] = {}
        for box in cell_boxes(self.grid, self.tile_size, self.gap):
            label, _ = _classify_classical(img.crop(box))
            counts[label] = counts.get(label, 0) + 1
        return max(counts, key=counts.get) if counts else "bus"

    def _solve(self, challenge: Challenge) -> Attempt:
        from bot.grid_solver import solve_grid
        return solve_grid(challenge)

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
    """Single-image CAPTCHA: click the object matching the target.

    Two attack modes:

    * **single** — one target object ("click the dog")
    * **multi** — every matching object ("select all the dogs")

    The semantic solver (`bot/image_solver.py`) handles both: it
    segments the scene into candidate objects and classifies each one
    with CLIP, so the target can be ANY word ("dog", "cat", "traffic
    light"), not a fixed label set. When the CLIP model is absent it
    degrades to the classical shape features.
    """

    name = "image"

    def __init__(self, target: str | None = None, multi: bool = False,
                 threshold: float = 0.22, use_clip: bool = False,
                 segmentation: str = "auto"):
        self.target = target
        self.multi = multi
        self.threshold = threshold
        self.use_clip = use_clip
        self.segmentation = segmentation

    def _build_challenge(self, img: Image.Image, **public) -> Challenge:
        # An explicit target always wins. Only infer one when the caller
        # did not say what to look for — inferring over a scene full of
        # distractor classes is how the attacker ends up clicking the
        # bus when the challenge asked for the bird.
        target = (public.get("target") or self.target
                  or self._infer_target(img))
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
        """Guess which class the scene holds, when the caller says nothing.

        Uses the attacker's own segmenter and classifier — no generator
        import — so this works on a field target too.
        """
        from bot import segment as segmenter
        from bot import shapes
        try:
            boxes = segmenter.segment(img, strategy="auto")
        except Exception:
            boxes = []
        if not boxes:
            return "bus"
        counts: dict[str, int] = {}
        for box in boxes:
            crop = img.crop((box["x"], box["y"],
                             box["x"] + box["w"], box["y"] + box["h"]))
            label, _ = shapes.classify(crop)
            counts[label] = counts.get(label, 0) + 1
        return max(counts, key=counts.get)

    def _solve(self, challenge: Challenge) -> Attempt:
        """Solve with the semantic (CLIP) attacker.

        The fallback to the lab's classical solver is kept for the case
        where the generator module happens to be installed, but the
        attacker never *requires* it.
        """
        from core.images import decode_data_url
        img = decode_data_url(challenge.public["image"])
        target = challenge.public.get("target") or self.target or "dog"

        from bot.image_solver import SemanticImageSolver
        sem = SemanticImageSolver(target=target, multi=self.multi,
                                  threshold=self.threshold,
                                  use_clip=self.use_clip,
                                  segmentation=self.segmentation)
        out = sem.solve(img)
        if out["hits"] or out["candidates"] == 0:
            hits = [h.as_dict() for h in out["hits"]]
            return Attempt(
                answer={"hits": hits, "n": len(hits)},
                meta={"strategy": out["strategy"],
                      "candidates": out["candidates"],
                      "target": target,
                      "multi": self.multi,
                      "confidence": max([h.score for h in out["hits"]],
                                        default=0.0),
                      "hits": hits},
            )

        # Nothing found. The attacker does NOT fall back to the
        # generator's solver: an attacker that calls the generator is
        # not measuring the challenge, it is reading the answer sheet.
        return Attempt(answer={"hits": [], "n": 0}, confidence=0.0,
                       meta={"reason": "no candidate matched the target",
                             "target": target})

    def _attempt_to_result(self, attempt: Attempt) -> SolverResult:
        meta = attempt.meta or {}
        hits = meta.get("hits")
        if hits is not None:
            # Semantic path: a list of selected objects.
            answer = {
                "hits": hits,
                "n": len(hits),
                "clicks": [[h["centre"][0], h["centre"][1]] for h in hits],
                "x": hits[0]["centre"][0] if hits else None,
                "y": hits[0]["centre"][1] if hits else None,
            }
            return SolverResult(
                family=self.name,
                answer=answer,
                confidence=float(meta.get("confidence", 0.0)),
                duration_ms=attempt.duration_ms,
                ok=bool(hits),
                meta=meta,
            )

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
    from core.images import to_data_url
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
        return ImageSolver(target=kwargs.get("target"),
                           multi=bool(kwargs.get("multi")),
                           threshold=float(kwargs.get("threshold", 0.22)),
                           use_clip=bool(kwargs.get("use_clip")))
    raise ValueError(f"unknown family: {family!r} "
                     f"(known: slider, text, grid, image)")


SOLVERS = ("slider", "text", "grid", "image")
