"""Shared abstractions for the multi-type CAPTCHA test bench.

A "challenge family" is one kind of CAPTCHA: slider, OCR text, image
selection, grid selection… Every family implements the same tiny contract so
the harness can treat them uniformly:

    generate()  -> Challenge      (payload for the browser, truth for the server)
    solve()     -> Attempt        (the attacker's answer)
    score()     -> bool           (did the attack succeed?)

The ground truth never leaves the server. `Challenge.truth` lives on the
server side only; `Challenge.public` is what gets serialised to the client.

This module is deliberately dependency-light: it must import without cv2 or
any OCR engine so the core test bench runs anywhere.
"""
from __future__ import annotations

import base64
import io
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from PIL import Image


# --------------------------------------------------------------------------
# payload helpers
# --------------------------------------------------------------------------

# The data-URL helpers live in `core/` alongside the contract — both the
# attacker and the generators need to read and write image payloads.
from core.images import decode_data_url, to_data_url  # noqa: E402,F401


# --------------------------------------------------------------------------
# contracts
# --------------------------------------------------------------------------

# The Challenge/Attempt contract lives in `core/` — the one module both
# the attacker and the generators share. See core/contract.py.
from core.contract import Attempt, Challenge  # noqa: E402,F401


class Family(Protocol):
    """The contract every challenge family implements."""
    name: str

    def generate(self, seed: int, **kwargs) -> Challenge:
        ...

    def solve(self, challenge: Challenge) -> Attempt:
        """Run the attacker. Must not mutate the challenge."""
        ...

    def score(self, challenge: Challenge, attempt: Attempt) -> bool:
        """Did the attempt solve the challenge?"""
        ...


# --------------------------------------------------------------------------
# shared image utilities
# --------------------------------------------------------------------------

def rand_seed() -> int:
    return secrets.randbelow(2 ** 31)


def hsv_jitter(img: Image.Image, hue: float = 0.0, sat: float = 0.0,
               val: float = 0.0) -> Image.Image:
    """Shift hue/saturation/value of an RGB image."""
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    hsv = np.asarray(Image.fromarray((arr * 255).astype(np.uint8)).convert("HSV"),
                     dtype=np.float32)
    hsv[..., 0] = (hsv[..., 0] + hue * 255) % 256
    hsv[..., 1] = np.clip(hsv[..., 1] * (1 + sat), 0, 255)
    hsv[..., 2] = np.clip(hsv[..., 2] * (1 + val), 0, 255)
    return Image.fromarray(np.asarray(hsv, dtype=np.uint8).astype(np.uint8),
                           "HSV").convert("RGB")


def perturb_image(img: Image.Image, seed: int, strength: float = 1.0) -> Image.Image:
    """Apply the classic image-CAPTCHA distortions: noise, blur, speckle."""
    rng = np.random.default_rng(seed)
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)

    if strength > 0:
        # gaussian noise
        noise = rng.normal(0, 14 * strength, arr.shape[:2])[..., None]
        arr = arr + noise

    if strength > 0.5:
        # salt & pepper speckle
        sp = rng.random(arr.shape[:2])
        arr[sp < 0.004 * strength] = 0
        arr[sp > 1 - 0.004 * strength] = 255

    out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    if strength > 0.5:
        from PIL import ImageFilter
        out = out.filter(ImageFilter.GaussianBlur(radius=0.4 * strength))

    # wave distortion: shift each row horizontally by a sine wave
    if strength > 0:
        amp = 3.0 * strength
        freq = 0.02 + 0.02 * rng.random()
        src = np.asarray(out, dtype=np.uint8)
        h, w = src.shape[:2]
        dst = np.zeros_like(src)
        for y in range(h):
            dx = int(amp * np.sin(freq * y + rng.random() * 6.28))
            dst[y] = np.roll(src[y], dx, axis=0)
        out = Image.fromarray(dst)

    return out


def cut_text_image(text: str, size: tuple[int, int], font_fill=(20, 20, 20),
                   bg=(245, 245, 240)) -> Image.Image:
    """Render text with PIL's built-in font at a large-ish size.

    Uses the default bitmap font scaled up, which keeps the lab dependency-free.
    """
    from PIL import ImageDraw, ImageFont
    w, h = size
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=max(14, h // 3))
    except TypeError:                       # older Pillow
        font = ImageFont.load_default()
    # measure and centre
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((w - tw) / 2 - bbox[0], (h - th) / 2 - bbox[1]), text,
              fill=font_fill, font=font)
    return img
