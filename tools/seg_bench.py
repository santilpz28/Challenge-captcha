"""Compare segmentation strategies on the image family.

Answers one question: does the attacker find the objects? Everything
downstream (classification, multi-select) is capped by segmentation, so
this isolates the bottleneck the bench reports at 33%.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import segment as segmenter          # noqa: E402
from bot import shapes                          # noqa: E402
from challenges.image_family import ImageFamily  # noqa: E402


def overlap_iou(a, b) -> float:
    ax0, ay0 = a["x"], a["y"]
    ax1, ay1 = ax0 + a["w"], ay0 + a["h"]
    bx0, by0 = b["x"], b["y"]
    bx1, by1 = bx0 + b["w"], by0 + b["h"]
    ix = max(0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union else 0.0


def centre_offset(box, truth_box):
    """Offset between two boxes' centres."""
    return (abs((box["x"] + box["w"] // 2) - (truth_box["x"] + truth_box["w"] // 2))
            + abs((box["y"] + box["h"] // 2) - (truth_box["y"] + truth_box["h"] // 2)))


def match_ratio(boxes, truth, iou_thr=0.5) -> float:
    """Fraction of true objects matched by a predicted box.

    The ground truth is the 72x72 window the object was pasted into,
    while the drawn shape occupies only its middle. Two boxes therefore
    describe the same object even when their IoU is well below 0.5.
    The test that matches reality is the distance between their
    CENTRES: a detection at the same centre is the same object, at
    whatever size the template happened to be.
    """
    if not truth:
        return 1.0
    used = set()
    hit = 0
    for t in truth:
        best, best_off = None, 10 ** 9
        for i, b in enumerate(boxes):
            if i in used:
                continue
            off = centre_offset(b, t)
            if off < best_off:
                best, best_off = i, off
        if best is not None and best_off <= 12:
            used.add(best)
            hit += 1
    return hit / len(truth)


def precision(boxes, truth, iou_thr=0.5) -> float:
    """Fraction of the predicted boxes that describe a real object."""
    if not boxes:
        return 1.0
    used = set()
    tp = 0
    for t in truth:
        best, best_off = None, 10 ** 9
        for i, b in enumerate(boxes):
            if i in used:
                continue
            off = centre_offset(b, t)
            if off < best_off:
                best, best_off = i, off
        if best is not None and best_off <= 12:
            used.add(best)
            tp += 1
    return tp / len(boxes)


def main(runs: int = 30) -> int:
    strategies = ["template", "color", "components", "auto"]
    stats = {s: {"rec": [], "prec": [], "n": [], "ms": []} for s in strategies}

    for seed in range(runs):
        fam = ImageFamily()
        ch = fam.generate(seed=seed)
        from core.images import decode_data_url
        img = decode_data_url(ch.public["image"])
        # Every object's ground-truth position, not just the target's.
        truth = (ch.artifacts or {}).get("positions") or []
        if not truth:
            continue
        for s in strategies:
            t0 = time.time()
            try:
                boxes = segmenter.segment(img, strategy=s)
            except Exception:
                boxes = []
            ms = (time.time() - t0) * 1000
            stats[s]["rec"].append(match_ratio(boxes, truth))
            stats[s]["prec"].append(precision(boxes, truth))
            stats[s]["n"].append(len(boxes))
            stats[s]["ms"].append(ms)

    print(f"\n=== segmentación sobre {runs} escenas de la familia image ===\n")
    print(f"{'estrategia':<14}{'recall':>9}{'precisión':>12}"
          f"{'cajas/escena':>14}{'ms':>8}")
    print("-" * 58)
    for s in strategies:
        d = stats[s]
        if not d["rec"]:
            continue
        print(f"{s:<14}{np.mean(d['rec']):>8.1%}"
              f"{np.mean(d['prec']):>12.1%}"
              f"{np.mean(d['n']):>14.1f}{np.mean(d['ms']):>8.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
