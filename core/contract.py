"""Shared contract between the attacker and the challenge generators.

This module is the **only** thing both sides share. The attacker must
not import the generator's internals (that would be cheating — it would
know how the challenge was made), and the generator must not import the
attacker (that would couple the test bench to the thing it measures).

So the contract lives here, in the middle: a small dataclass pair that
describes "what a challenge is" and "what an answer looks like".
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Attempt:
    """What an attacker submits for a challenge."""

    answer: Any
    # Behavioural signals the client would report. The solver fills these
    # in so the behavioural gate has something to score.
    trace: list[dict] = field(default_factory=list)
    duration_ms: float = 0.0
    pointer_events: int = 0
    # Free-form diagnostics (per-cell confidences, OCR text, ...)
    meta: dict = field(default_factory=dict)


@dataclass
class Challenge:
    """A generated challenge.

    ``truth`` never leaves the server. ``public`` is the client-safe
    payload.
    """

    family: str
    cid: str
    seed: int
    created: float
    # Client-facing payload.
    public: dict
    # Server-side answer key.
    truth: Any
    # Reference images kept for scoring (e.g. the pristine tiles).
    artifacts: dict = field(default_factory=dict)

    @staticmethod
    def new_id() -> str:
        return secrets.token_urlsafe(12)

    @property
    def age_s(self) -> float:
        return time.time() - self.created
