"""Image payload helpers shared by the attacker and the generators.

CAPTCHA images travel as data URLs so the widget needs one request. Both
sides need to read and write that format, so it lives here — in the
neutral middle, not in either the attacker or the generator.
"""
from __future__ import annotations

import base64
import io

from PIL import Image


def to_data_url(img: Image.Image, fmt: str = "PNG") -> str:
    """Serialise a PIL image as a data URL (keeps the widget to one request)."""
    buf = io.BytesIO()
    img.save(buf, format=fmt, optimize=True)
    return "data:image/{};base64,{}".format(
        fmt.lower(), base64.b64encode(buf.getvalue()).decode())


def decode_data_url(url: str) -> Image.Image:
    """Inverse of `to_data_url`."""
    raw = base64.b64decode(url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)).convert("RGB")
