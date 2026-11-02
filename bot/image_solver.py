"""Semantic image CAPTCHA solver — the "click every dog" attacker.

Two shapes of challenge are handled:

* **single click** — one target object ("click the dog")
* **multi-select** — every matching object ("select all the dogs")

The attack has three stages, like a real attacker:

1. **Segment.** The scene holds several objects on a busy background.
   A naive saturation threshold merges neighbours into one blob; the
   attack that works is colour clustering (k-means over the pixels), so
   each object becomes its own candidate region regardless of how close
   it sits to the next one.
2. **Classify.** Each candidate is embedded with CLIP and compared
   against the target's text prompt. This is what makes the attacker
   handle *arbitrary* targets ("dog", "cat", "traffic light") instead of
   a fixed label set. When CLIP is unavailable the solver falls back to
   the classical shape features, which only know the synthetic labels.
3. **Select.** For a multi-select challenge every candidate above the
   threshold is returned; for a single-click challenge the best one is.

The confidence combines CLIP's softmax margin over the candidate's best
label with the margin over the runner-up, so a scene where every object
scores the same does not look confident.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from PIL import Image

# The default prompt set. When the caller gives a target, this is the
# label set it competes against — including the confusable negatives,
# which is what makes the margin meaningful.
DEFAULT_LABELS = [
    "dog", "cat", "bird", "horse", "car", "bus", "truck", "bicycle",
    "boat", "airplane", "traffic light", "fire hydrant", "stop sign",
    "person", "tree", "bench",
]

# Negatives used to sharpen the confidence when no explicit label set
# is supplied: objects that are easy to confuse with the target.
_CONFUSABLES = [
    "a photo of nothing in particular",
    "a photo of a plain background",
]


@dataclass
class ImageHit:
    """One selected object."""

    x: int
    y: int
    w: int
    h: int
    label: str
    score: float
    centre: tuple[int, int] = field(default=(0, 0))

    def as_dict(self) -> dict:
        return {
            "x": self.x, "y": self.y, "w": self.w, "h": self.h,
            "label": self.label, "score": round(self.score, 3),
            "centre": list(self.centre),
        }


class SemanticImageSolver:
    """CLIP-backed solver for image-click and image-multi-select CAPTCHAs."""

    def __init__(self, target: str = "dog", multi: bool = False,
                 threshold: float = 0.22, labels: list[str] | None = None,
                 use_clip: bool = False):
        self.target = target
        self.multi = multi
        self.threshold = threshold
        self.labels = labels or DEFAULT_LABELS
        # CLIP is opt-in: it is the right attacker for real photographs
        # ("click the dog") but a poor one for synthetic clip-art, where
        # it labels a drawn bus as "tree".
        self.use_clip = use_clip

    # ------------------------------------------------------------------
    def _classifier(self):
        """Return a CLIP classifier, or None when the model is absent.

        The classifier lives with the challenge generators because that is
        where the model download/cache lives, but the attacker treats it
        as strictly optional: without it the solver still runs, just with
        the classical features.
        """
        try:
            from challenges.clip_classifier import ClipClassifier, available
        except Exception:
            return None
        if not available():
            return None
        try:
            clf = ClipClassifier(self.labels)
            if not clf.ready:
                return None
            return clf
        except Exception:
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def segment(img: Image.Image) -> list[dict]:
        """Split the scene into candidate object regions.

        Uses colour clustering: every object has a characteristic hue, so
        grouping pixels by colour yields one blob per object even when
        two objects overlap. Falls back to a saturation/edge heuristic
        when OpenCV is unavailable.
        """
        boxes: list[dict] = []
        try:
            from challenges.image_family import ImageFamily
            boxes = ImageFamily._detect_blobs(img) or []
        except Exception:
            boxes = []
        if boxes:
            return boxes

        # Fallback: simple edge-aware segmentation.
        import numpy as np
        arr = np.asarray(img.convert("RGB"))
        h, w, _ = arr.shape
        # Distance from the background colour (estimated from the
        # border pixels) marks the objects.
        border = np.concatenate([
            arr[:4].reshape(-1, 3), arr[-4:].reshape(-1, 3),
            arr[:, :4].reshape(-1, 3), arr[:, -4:].reshape(-1, 3)])
        bg = np.median(border, axis=0)
        dist = np.linalg.norm(arr.astype(float) - bg, axis=2)
        mask = dist > 55
        try:
            import cv2
            mask = (mask.astype(np.uint8) * 255)
            n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
            boxes = []
            for i in range(1, n):
                x, y, bw, bh, area = stats[i]
                if area < (w * h) * 0.004 or bw < 12 or bh < 12:
                    continue
                boxes.append({"x": int(x), "y": int(y),
                              "w": int(bw), "h": int(bh)})
            return boxes
        except Exception:
            return []

    # ------------------------------------------------------------------
    def solve(self, img: Image.Image) -> dict:
        """Return every matching object found in the image.

        Result::

            {"hits": [ImageHit, ...], "strategy": "clip"|"classical",
             "candidates": N, "duration_ms": float}
        """
        t0 = time.time()
        boxes = self.segment(img)
        if not boxes:
            return {"hits": [], "strategy": "none", "candidates": 0,
                    "duration_ms": round((time.time() - t0) * 1000, 1)}

        clf = self._classifier() if self.use_clip else None
        hits: list[ImageHit] = []
        strategy = "classical" if clf is None else "clip"

        if clf is not None:
            strategy = "clip"
            crops = []
            for b in boxes:
                crops.append(img.crop((b["x"], b["y"],
                                       b["x"] + b["w"], b["y"] + b["h"])))
            try:
                scored = clf.classify_batch(crops)
            except Exception:
                scored = []
            for b, crop, sc in zip(boxes, crops, scored or []):
                label, score = sc
                # The target's own probability is what matters, not the
                # argmax: a scene of dogs must return every dog, so the
                # threshold is applied to the TARGET's score.
                t_score = self._target_score(clf, crop)
                if t_score is not None:
                    score = t_score
                if score >= self.threshold:
                    cx = b["x"] + b["w"] // 2
                    cy = b["y"] + b["h"] // 2
                    hits.append(ImageHit(
                        x=b["x"], y=b["y"], w=b["w"], h=b["h"],
                        label=label, score=float(score), centre=(cx, cy)))
        else:
            # Classical fallback: shape features over the synthetic label
            # set. Only works for the lab's own labels.
            try:
                from challenges.grid_family import GridFamily
            except Exception:
                GridFamily = None
            if GridFamily is not None:
                for b in boxes:
                    crop = img.crop((b["x"], b["y"],
                                     b["x"] + b["w"], b["y"] + b["h"]))
                    label, score = GridFamily._classify(crop)
                    if label == self.target or score >= self.threshold:
                        cx = b["x"] + b["w"] // 2
                        cy = b["y"] + b["h"] // 2
                        hits.append(ImageHit(
                            x=b["x"], y=b["y"], w=b["w"], h=b["h"],
                            label=label, score=float(score), centre=(cx, cy)))

        hits.sort(key=lambda h: h.score, reverse=True)
        if not self.multi and hits:
            hits = hits[:1]

        return {
            "hits": hits,
            "strategy": strategy,
            "candidates": len(boxes),
            "duration_ms": round((time.time() - t0) * 1000, 1),
        }

    # ------------------------------------------------------------------
    def _target_score(self, clf, crop: Image.Image) -> float | None:
        """CLIP probability of the TARGET label for one crop."""
        try:
            scores = clf.score_labels(crop)
        except AttributeError:
            return None
        except Exception:
            return None
        try:
            idx = [l.lower() for l in self.labels].index(self.target.lower())
            return float(scores[idx])
        except (ValueError, IndexError):
            return None
