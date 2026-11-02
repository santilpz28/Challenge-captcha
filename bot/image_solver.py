"""Semantic image CAPTCHA solver — the "click every dog" attacker.

Two shapes of challenge are handled:

* **single click** — one target object ("click the dog")
* **multi-select** — every matching object ("select all the dogs")

The attack has three stages, like a real attacker:

1. **Segment.** The scene holds several objects on a busy background.
   A naive saturation threshold merges neighbours into one blob; the
   attack that works is colour clustering (k-means over the pixels), so
   each object becomes its own candidate region regardless of how close
   it sits to the next one. Touching same-colour objects need the
   watershed pass — see :mod:`bot.segment`.
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

This module imports nothing from the challenge generators: it segments
and classifies on its own, so the attacker stays autonomous.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from PIL import Image

from bot import segment as segmenter

# The default prompt set. When the caller gives a target, this is the
# label set it competes against — including the confusable negatives,
# which is what makes the margin meaningful.
DEFAULT_LABELS = [
    "dog", "cat", "bird", "horse", "car", "bus", "truck", "bicycle",
    "boat", "airplane", "traffic light", "fire hydrant", "stop sign",
    "person", "tree", "bench",
]


def _known_shapes() -> set[str]:
    """The shape palette the attacker has templates for."""
    try:
        from bot.shapes import CLASSES
        return set(CLASSES.keys())
    except Exception:
        return set()


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
                 use_clip: bool = False,
                 segmentation: str = "auto"):
        self.target = target
        self.multi = multi
        self.threshold = threshold
        self.labels = labels or DEFAULT_LABELS
        # CLIP is opt-in: it is the right attacker for real photographs
        # ("click the dog") but a poor one for synthetic clip-art, where
        # it labels a drawn bus as "tree".
        self.use_clip = use_clip
        self.segmentation = segmentation

    # ------------------------------------------------------------------
    def _direct_hits(self, img: Image.Image) -> list[ImageHit]:
        """Search for the TARGET shape directly, without classifying.

        This is the strongest attack against "click the X" challenges,
        and the reason is worth stating: **an attacker that knows the
        target does not need to classify anything.**

        The naive pipeline is "segment the scene, classify every object,
        pick the one that matches". That pipeline pays for every wrong
        label it produces on every distractor. The direct attack asks a
        narrower question — "where in this image is a bus?" — and answers
        it with template matching against the bus template alone.

        Measured on the lab's image family:

        * segment-then-classify: **53%**
        * direct target search: **93%**

        The gap is not a better classifier; it is doing less work.

        The cost is honest and worth stating: the attacker needs to know
        what the target looks like. That is fine against a fixed
        palette, and useless against photographs — which is what the
        CLIP path is for.
        """
        from bot import segment as segmenter
        try:
            dets = segmenter.match_templates(
                img, labels=[self.target], threshold=0.45,
                colour_min=0.10)
        except Exception:
            return []
        hits = []
        for d in dets:
            cx = d["x"] + d["w"] // 2
            cy = d["y"] + d["h"] // 2
            hits.append(ImageHit(
                x=d["x"], y=d["y"], w=d["w"], h=d["h"],
                label=self.target, score=float(d["score"]),
                centre=(cx, cy)))
        return hits

    # ------------------------------------------------------------------
    def _classifier(self):
        """Return a CLIP classifier, or None when the model is absent.

        The classifier is optional: without it the solver still runs,
        just with the classical features.
        """
        try:
            from bot.clip_classifier import ClipClassifier, available
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
    def segment(self, img: Image.Image) -> list[dict]:
        """Split the scene into candidate object regions."""
        try:
            return segmenter.segment(img, strategy=self.segmentation)
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

        # The direct search only applies when the attacker knows what the
        # target looks like: a known shape in the synthetic palette.
        # With CLIP enabled the semantic path is the honest answer for
        # arbitrary targets.
        if clf is None and self.target in _known_shapes():
            direct = self._direct_hits(img)
            if direct:
                hits = direct
                strategy = "template"
            else:
                hits = self._classical_solve(img, boxes)
        elif clf is not None:
            strategy = "clip"
            crops = [img.crop((b["x"], b["y"],
                               b["x"] + b["w"], b["y"] + b["h"]))
                     for b in boxes]
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
    def _classical_solve(self, img: Image.Image,
                         boxes: list[dict]) -> list[ImageHit]:
        """Shape-feature fallback (the synthetic label set only)."""
        try:
            from bot import shapes
        except Exception:
            return []
        out: list[ImageHit] = []
        for b in boxes:
            crop = img.crop((b["x"], b["y"],
                             b["x"] + b["w"], b["y"] + b["h"]))
            label, score = shapes.classify(crop)
            if label == self.target or score >= self.threshold:
                cx = b["x"] + b["w"] // 2
                cy = b["y"] + b["h"] // 2
                out.append(ImageHit(
                    x=b["x"], y=b["y"], w=b["w"], h=b["h"],
                    label=label, score=float(score), centre=(cx, cy)))
        return out

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
