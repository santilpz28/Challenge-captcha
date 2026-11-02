"""Text CAPTCHA attacker — OCR with multi-binarisation voting.

The chain is: denoise -> upscale -> try several binarisations -> keep
whichever the OCR engine reads best. An attacker does not know which
binarisation the site used, so trying all of them is the cheap and
correct move.

The engine is RapidOCR when available (ONNX, CPU-friendly, no API key);
it falls back to pytesseract if the user has it. With neither, the
solver reports that it cannot read text rather than guessing.
"""
from __future__ import annotations

import time

import numpy as np
from PIL import Image

from core.contract import Attempt, Challenge
from core.images import decode_data_url

# The character set the lab's text challenges use. An attacker infers
# the allowed set from the challenge payload when the site publishes it
# and falls back to this one otherwise.
DEFAULT_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

_ENGINE = None
_ENGINE_TRIED = False
_ENGINE_BACKEND = None
_ENGINE_LOCK = None


def _ocr_engine():
    """Return (engine, backend) or (None, None).

    The engine is cached, but the cache is only written on SUCCESS. A
    failed initialisation (a transient ONNX/model-path hiccup, two
    processes loading the same model at once) must not poison the
    process for the rest of its life — that is exactly the bug that made
    the OCR silently return nothing.
    """
    global _ENGINE, _ENGINE_TRIED, _ENGINE_BACKEND
    if _ENGINE_TRIED and _ENGINE is not None:
        return _ENGINE, _ENGINE_BACKEND
    if _ENGINE_TRIED and _ENGINE is None:
        return None, None

    try:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
        _ENGINE_BACKEND = "rapidocr"
        _ENGINE_TRIED = True
        return _ENGINE, _ENGINE_BACKEND
    except Exception:
        pass
    try:
        import pytesseract  # noqa: F401
        _ENGINE = pytesseract
        _ENGINE_BACKEND = "pytesseract"
        _ENGINE_TRIED = True
        return _ENGINE, _ENGINE_BACKEND
    except Exception:
        pass
    _ENGINE_TRIED = True
    return None, None


def engine_available() -> bool:
    return _ocr_engine()[0] is not None


def _read(engine, backend: str, img: np.ndarray) -> list:
    """Return a list of (box, text, score) tuples for one image."""
    if backend == "rapidocr":
        res, _ = engine(img)
        out = []
        for item in (res or []):
            # RapidOCR returns [box, text, score] per detection.
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                text = item[1]
                if isinstance(text, str):
                    out.append(item)
        return out
    # pytesseract returns a string; normalise it to the same shape.
    text = engine.image_to_string(img)
    return [(None, ch, 0.0) for ch in text]


def preprocess(img: np.ndarray) -> list[np.ndarray]:
    """Candidate binarisations of the challenge image."""
    try:
        import cv2
    except ImportError:
        return [img]

    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    g = cv2.medianBlur(g, 3)
    g = cv2.resize(g, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

    out = []
    # adaptive threshold (handles uneven background)
    out.append(cv2.cvtColor(cv2.adaptiveThreshold(
        g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 31, 12), cv2.COLOR_GRAY2RGB))
    # Otsu
    _, otsu = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    out.append(cv2.cvtColor(otsu, cv2.COLOR_GRAY2RGB))
    # raw upscaled grayscale
    out.append(cv2.cvtColor(g, cv2.COLOR_GRAY2RGB))
    return out


def solve_text(challenge: Challenge, alphabet: str | None = None) -> Attempt:
    """Read the challenge with OCR."""
    engine, backend = _ocr_engine()
    if engine is None:
        return Attempt(answer="", duration_ms=0.0, pointer_events=0,
                       meta={"error": "OCR engine unavailable "
                                     "(pip install rapidocr_onnxruntime)"})
    chars = alphabet or challenge.public.get("charset") or DEFAULT_ALPHABET

    t0 = time.time()
    img = np.asarray(decode_data_url(challenge.public["image"]))
    best_text, best_score = "", -1
    for v in preprocess(img):
        try:
            res = _read(engine, backend, v)
        except Exception:
            continue
        if not res:
            continue
        txt = "".join(r[1] for r in res).strip().upper()
        score = sum(1 for c in txt if c in chars)
        if score > best_score:
            best_score, best_text = score, txt

    # Keep only characters in the challenge's charset: OCR often emits
    # punctuation for noise, and an attacker submitting them would just
    # fail a validation the server would reject anyway.
    cleaned = "".join(c for c in best_text if c in chars)
    cleaned = _drop_ocr_duplicates(cleaned)
    return Attempt(
        answer=cleaned,
        duration_ms=round((time.time() - t0) * 1000, 1),
        pointer_events=len(cleaned),
        meta={"ocr_raw": best_text, "ocr_text": cleaned,
              "charset_hits": best_score, "engine": backend},
    )


def _drop_ocr_duplicates(text: str) -> str:
    """Remove the duplicate characters OCR loves to invent.

    A noisy glyph read twice is a classic OCR artefact: "CFHF35" comes
    back as "CFHF335". The rule is simple and conservative:

    * a run of exactly 2 identical characters collapses to 1 (the
      duplicate is the artefact),
    * a run of 3 or more is left alone — "111" is a legitimate answer,
      and collapsing it would break a real one.

    OCR duplication is almost always a pair, so this removes the noise
    without touching deliberate repetition.
    """
    if not text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        j = i
        while j < n and text[j] == ch:
            j += 1
        run = j - i
        out.append(ch * (run if run >= 3 else 1))
        i = j
    return "".join(out)


def solve_image_text(img: Image.Image, alphabet: str | None = None) -> Attempt:
    """Solve a text CAPTCHA given a raw image instead of a Challenge."""
    tmp = Challenge(family="text", cid=Challenge.new_id(), seed=0,
                    created=time.time(),
                    public={"image": _to_data_url(img),
                            "charset": alphabet or DEFAULT_ALPHABET},
                    truth={})
    return solve_text(tmp, alphabet)


def _to_data_url(img: Image.Image) -> str:
    from core.images import to_data_url
    return to_data_url(img)
