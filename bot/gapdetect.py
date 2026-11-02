"""Gap detection by local contrast — the second slider strategy.

**Why two strategies?** A real attacker cannot assume the site's sprite is
the pristine texture of the gap. Many sliders ship a *decorated* piece
(keyline, drop shadow, rounded decoration) and a gap whose interior was
re-painted or blurred. Correlating the sprite against the background then
fails or — worse — peaks somewhere plausible but wrong.

The fallback is to stop relying on the sprite entirely and find the gap
**by its shape in the image**: a slider gap is a region whose interior is
darker (or lighter) than its immediate surroundings, bounded by a hard
outline.

The method is classical and cheap:

  1. grayscale the board,
  2. subtract a heavily blurred copy of itself → local contrast,
  3. threshold the *negative* part (pixels darker than their neighbourhood),
  4. label the connected components,
  5. score each blob by how much of its bounding box is filled and how
     square-ish it is — a slider gap is a compact, roughly square region,
  6. return the blob's centre.

This is the same idea as the lab's `use_hole_detector` flag, but promoted to
a first-class strategy with its own confidence, because in the field it is
the one that generalises.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

try:                                              # optional but present in CI
    from scipy import ndimage as _nd
    _HAVE_SCIPY = True
except Exception:                                 # pragma: no cover
    _HAVE_SCIPY = False


def _largest_blob_fallback(mask: np.ndarray) -> tuple[int, int, int, int]:
    """Connected components without scipy (simple flood fill)."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best = (0, (0, 0, 0, 0))
    for y in range(h):
        for x in range(w):
            if not mask[y, x] or seen[y, x]:
                continue
            stack = [(y, x)]
            seen[y, x] = True
            xs, ys = [], []
            while stack:
                cy, cx = stack.pop()
                xs.append(cx)
                ys.append(cy)
                for ny, nx in ((cy - 1, cx), (cy + 1, cx),
                                (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] \
                            and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if len(xs) > best[0]:
                best = (len(xs), (min(xs), min(ys), max(xs), max(ys)))
    _, (x0, y0, x1, y1) = best
    return x0, y0, x1, y1


def detect_gap(board: Image.Image, dark: bool = True, threshold: float = 12.0,
               window: int = 25) -> dict:
    """Locate the slider gap in `board` by local contrast.

    Returns ``{"x", "y", "confidence", "bbox", "candidates"}`` where ``x``
    and ``y`` are the **centre of the gap's bounding box** — the same
    reference a verify endpoint uses.

    ``dark=True`` looks for a gap darker than its surroundings (the usual
    case: a hole in a lit surface). ``dark=False`` inverts it.
    """
    grey = np.asarray(board.convert("L"), dtype=np.float64)
    h, w = grey.shape

    if _HAVE_SCIPY:
        blur = _nd.uniform_filter(grey, size=window)
    else:
        # Box blur via cumulative sums — same result, no dependency.
        k = window
        pad = k // 2
        padded = np.pad(grey, pad, mode="edge")
        cs = padded.cumsum(axis=0).cumsum(axis=1)
        cs = np.pad(cs, ((1, 0), (1, 0)), mode="constant")
        blur = (cs[k:, k:] - cs[:-k, k:] - cs[k:, :-k] + cs[:-k, :-k]) / (k * k)
        blur = blur[:h, :w]

    contrast = (blur - grey) if dark else (grey - blur)
    mask = contrast > threshold

    if _HAVE_SCIPY:
        labels, n = _nd.label(mask)
        if n == 0:
            return {"x": None, "y": None, "confidence": 0.0, "bbox": None,
                    "candidates": [], "error": "no gap-like region found"}
        sizes = _nd.sum(mask, labels, range(1, n + 1))
        objects = _nd.find_objects(labels)
    else:
        x0, y0, x1, y1 = _largest_blob_fallback(mask)
        if x1 <= x0 or y1 <= y0:
            return {"x": None, "y": None, "confidence": 0.0, "bbox": None,
                    "candidates": [], "error": "no gap-like region found"}
        objects = [(slice(y0, y1 + 1), slice(x0, x1 + 1))]
        n = 1
        sizes = np.array([float((x1 - x0 + 1) * (y1 - y0 + 1))])
        labels = None

    candidates = []
    for i in range(n):
        sl = objects[i]
        if sl is None:
            continue
        size = float(sizes[i])
        if size < 120:                # ignore specks
            continue
        y0, y1 = sl[0].start, sl[0].stop
        x0, x1 = sl[1].start, sl[1].stop
        bw, bh = x1 - x0, y1 - y0
        if bw < 8 or bh < 8:
            continue
        # A slider gap is a compact region: its pixels fill a good part of
        # their bounding box, and the box is roughly square.
        fill = size / float(bw * bh)
        aspect = bw / float(bh)
        squareness = min(aspect, 1.0 / aspect)
        score = fill * squareness * (size ** 0.5)
        candidates.append({
            "x": int((x0 + x1) / 2), "y": int((y0 + y1) / 2),
            "bbox": (int(x0), int(y0), int(x1), int(y1)),
            "size": int(size), "fill": round(fill, 3),
            "aspect": round(aspect, 3), "score": round(score, 2),
        })

    if not candidates:
        return {"x": None, "y": None, "confidence": 0.0, "bbox": None,
                "candidates": [], "error": "no gap-like region found"}

    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0]
    runner = candidates[1]["score"] if len(candidates) > 1 else 0.0
    # Confidence: how well the winner separates from the runner-up, tempered
    # by how square and solid the blob is.
    margin = (best["score"] - runner) / best["score"] if best["score"] else 0.0
    shape_q = best["fill"] * min(best["aspect"], 1.0 / best["aspect"])
    confidence = max(0.0, min(1.0, 0.4 * margin + 0.6 * shape_q))

    return {
        "x": best["x"], "y": best["y"],
        "confidence": round(confidence, 4),
        "bbox": best["bbox"], "candidates": candidates[:5],
        "error": None,
    }
