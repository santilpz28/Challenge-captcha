"""Grid CAPTCHA attacker — "select all the dogs" across a tile grid.

The challenge is a grid of tiles; the target appears in some of them and
the answer is the set of matching cells. Two attackers are implemented:

* **semantic (CLIP)** — each tile is embedded and compared against the
  target's text prompt. This is what handles an arbitrary target
  ("dog", "cat", "traffic light") and it is the stronger attack.
* **classical** — shape features (Hu moments, fill ratio, aspect) over a
  fixed palette. Cheap, CPU-only, no model download, but it only knows
  the lab's synthetic labels.

The semantic one wins when the model is available; the classical one
keeps the tool usable with zero setup, which matters for a field tool.
"""
from __future__ import annotations

import time

from PIL import Image

from core.contract import Attempt, Challenge
from core.images import decode_data_url


def cell_boxes(grid: int, cell: int, gap: int) -> list[tuple[int, int, int, int]]:
    """The crop box of every cell, in reading order."""
    out = []
    for i in range(grid * grid):
        r, c = divmod(i, grid)
        x0 = c * (cell + gap)
        y0 = r * (cell + gap)
        out.append((x0, y0, x0 + cell, y0 + cell))
    return out


def solve_grid(challenge: Challenge, use_clip: bool = False) -> Attempt:
    """Classify every cell and select the ones matching the target.

    ``use_clip`` switches to the semantic attacker. It is off by default
    because CLIP reads photographs, not the synthetic clip-art this
    bench generates — on drawn shapes it is actively worse than the
    classical features (it labels a drawn car "tree").
    """
    t0 = time.time()
    img = decode_data_url(challenge.public["image"])
    grid = int(challenge.public.get("grid", 3))
    cell = int(challenge.public.get("tile_size", 96))
    gap = int(challenge.public.get("gap", 8))
    target = challenge.public.get("target") or "dog"

    boxes = cell_boxes(grid, cell, gap)
    tiles = [img.crop(b) for b in boxes]

    scores: dict[int, tuple[str, float]] = {}
    picked: list[int] = []
    strategy = "classical"

    # --- semantic pass (opt-in) ----------------------------------------
    # CLIP is a strong attacker on REAL photographs ("click the dog"),
    # and a poor one on synthetic clip-art: it happily labels a drawn
    # car as "tree". So the semantic pass only runs when explicitly
    # asked for, and the classical pass is the default.
    if use_clip:
        clf = _clip_classifier()
        if clf is not None:
            strategy = "clip"
            try:
                dist = clf.score_labels_batch(tiles)
            except Exception:
                dist = []
            if dist:
                try:
                    t_idx = [l.lower() for l in clf.labels].index(target.lower())
                except ValueError:
                    t_idx = None
                for i, row in enumerate(dist):
                    if t_idx is None:
                        label = clf.labels[int(max(range(len(row)),
                                                   key=lambda k: row[k]))]
                        score = float(max(row))
                    else:
                        label = target
                        score = float(row[t_idx])
                    scores[i] = (label, round(score, 4))
                    if score >= _GRID_THRESHOLD:
                        picked.append(i)

    # --- classical pass ------------------------------------------------
    if not scores:
        strategy = "classical"
        for i, tile in enumerate(tiles):
            label, score = _classify_classical(tile)
            scores[i] = (label, round(score, 4))
            if label == target:
                picked.append(i)

    return Attempt(
        answer=sorted(picked),
        duration_ms=round((time.time() - t0) * 1000, 1),
        pointer_events=len(picked),
        meta={"cells_scored": scores,
              "target": target,
              "n_picked": len(picked),
              "strategy": strategy},
    )


_GRID_THRESHOLD = 0.22


def _clip_classifier():
    """Return a CLIP classifier over the standard label set, or None."""
    try:
        from bot.image_solver import DEFAULT_LABELS
        from challenges.clip_classifier import ClipClassifier, available
    except Exception:
        return None
    if not available():
        return None
    try:
        clf = ClipClassifier(DEFAULT_LABELS)
        return clf if clf.ready else None
    except Exception:
        return None


def _classify_classical(tile: Image.Image) -> tuple[str, float]:
    """Shape-feature classifier over the lab's palette.

    Falls back to a generic colour-similarity score when the generator
    module is not present (which is the normal case for a field tool).
    """
    try:
        from challenges.grid_family import GridFamily
        return GridFamily._classify(tile)
    except Exception:
        return _colour_signature(tile)


def _colour_signature(tile: Image.Image) -> tuple[str, float]:
    """Generic fallback: describe a tile by its dominant colour.

    Returns a synthetic label ("c0".."c5") and a confidence. It cannot
    know what a "dog" is, but it CAN tell whether two tiles are the same
    object — enough for the "select all the X" pattern when the target
    is the majority class.
    """
    try:
        import numpy as np
        arr = np.asarray(tile.convert("RGB").resize((32, 32)))
        mean = arr.reshape(-1, 3).mean(axis=0)
        r, g, b = [float(v) / 255.0 for v in mean]
        mx, mn = max(r, g, b), min(r, g, b)
        if mx - mn < 0.08:
            label = "c_gray"
        elif r == mx:
            label = "c_red" if g < b else "c_magenta"
        elif g == mx:
            label = "c_green" if r < b else "c_yellow"
        else:
            label = "c_blue" if r < g else "c_cyan"
        sat = (mx - mn) / (mx + 1e-6)
        return label, float(min(0.95, 0.35 + sat * 0.6))
    except Exception:
        return "unknown", 0.0
