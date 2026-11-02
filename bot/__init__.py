"""Challenge Captcha Bot — automated CAPTCHA solving.

A unified entrypoint over four challenge families:

    slider   drag-to-fit jigsaw   -> masked NCC over FFT (pure numpy)
    text     "type the letters"  -> RapidOCR (PP-OCRv4 ONNX)
    grid     "select all X"       -> shape classifier (Hu moments)
    image    "click the X"        -> hue clustering + shape classifier

Everything runs locally. No target-specific training, no cloud OCR, no
API keys. The attacker has never seen the target's font before.

Legal scope: use this against systems you own or have written authorisation
to test. See LICENSE and README ("Authorization") for details.
"""
from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
