"""HTTP client for talking to a live CAPTCHA endpoint.

Some sites expose their challenge through a JSON API rather than a canvas
widget (that is exactly the weakness the lab measures: the `api_only`
attack wins 100% against a client-trusted endpoint). This module speaks
that dialect: fetch a challenge, solve it, submit it back.

It is intentionally conservative about what it does:

* it never guesses API shapes — you tell it where the fields are,
* it records every request/response so an auditor can replay the exchange,
* it refuses to submit without an explicit ``submit=True``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import urllib.error
import urllib.request


@dataclass
class Exchange:
    """One HTTP round-trip, kept for the audit trail."""

    method: str
    url: str
    status: int = 0
    request_body: Any = None
    response_body: Any = None
    elapsed_ms: float = 0.0
    error: str | None = None
    headers: dict = field(default_factory=dict)


@dataclass
class LiveChallenge:
    """A challenge fetched from a live endpoint."""

    raw: dict
    image: bytes | None = None
    piece: bytes | None = None
    challenge_id: str | None = None
    meta: dict = field(default_factory=dict)


class CaptchaClient:
    """Minimal HTTP client for CAPTCHA challenge APIs."""

    def __init__(self, base_url: str, timeout: float = 15.0,
                 headers: dict | None = None,
                 user_agent: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent or (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        self.headers = {"User-Agent": self.user_agent,
                        "Accept": "application/json, text/plain, */*"}
        if headers:
            self.headers.update(headers)
        self.exchanges: list[Exchange] = []

    # ------------------------------------------------------------------
    # low level
    # ------------------------------------------------------------------
    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def request(self, method: str, path: str, body: Any = None,
                raw: bool = False) -> Exchange:
        url = self._url(path)
        data = None
        headers = dict(self.headers)
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method.upper())
        ex = Exchange(method=method.upper(), url=url, request_body=body)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
                ex.status = resp.status
                ex.headers = dict(resp.headers)
                if raw:
                    ex.response_body = payload
                else:
                    try:
                        ex.response_body = json.loads(payload.decode("utf-8"))
                    except Exception:
                        ex.response_body = payload.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            ex.status = e.code
            ex.error = f"HTTP {e.code}"
            try:
                ex.response_body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
        except Exception as e:
            ex.error = f"{type(e).__name__}: {e}"
        ex.elapsed_ms = round((time.time() - t0) * 1000, 1)
        self.exchanges.append(ex)
        return ex

    def get(self, path: str, raw: bool = False) -> Exchange:
        return self.request("GET", path, raw=raw)

    def post(self, path: str, body: Any, raw: bool = False) -> Exchange:
        return self.request("POST", path, body=body, raw=raw)

    # ------------------------------------------------------------------
    # challenge handling
    # ------------------------------------------------------------------
    def fetch_challenge(self, path: str = "/api/challenge",
                        image_field: str = "background",
                        piece_field: str | None = "piece",
                        id_field: str = "challenge_id") -> LiveChallenge:
        """GET a challenge and pull the image(s) out of the response.

        ``image_field``/``piece_field``/``id_field`` name the JSON keys.
        Images may be raw base64 or a ``data:`` URL.
        """
        ex = self.get(path)
        if ex.error or not isinstance(ex.response_body, dict):
            raise RuntimeError(f"challenge fetch failed: {ex.error or 'bad payload'}")

        payload = ex.response_body
        img = _decode_image(payload.get(image_field))
        piece = _decode_image(payload.get(piece_field)) if piece_field else None
        cid = payload.get(id_field)
        return LiveChallenge(raw=payload, image=img, piece=piece,
                             challenge_id=cid,
                             meta={"challenge": payload.get("challenge", {}),
                                   "elapsed_ms": ex.elapsed_ms})

    def submit(self, path: str, answer: Any, challenge_id: str | None = None,
               extra: dict | None = None, submit: bool = False,
               flat: bool = True) -> Exchange:
        """POST an answer. Requires ``submit=True`` — no accidental writes.

        ``flat=True`` (default) puts the answer fields at the top level of
        the body, which is what most verify endpoints expect::

            {"challenge_id": "...", "x": 174, "y": 81}

        ``flat=False`` nests everything under ``answer`` instead, for
        endpoints that use that shape.
        """
        if not submit:
            raise PermissionError(
                "submit() is a write operation; pass submit=True to confirm")

        if flat and isinstance(answer, dict):
            body: dict = dict(answer)
            if challenge_id:
                body.setdefault("challenge_id", challenge_id)
        else:
            body = {"answer": answer}
            if challenge_id:
                body["challenge_id"] = challenge_id

        if extra:
            body.update(extra)
        return self.post(path, body)

    def audit_trail(self) -> list[dict]:
        """The full exchange log — paste it into an audit report."""
        return [
            {
                "method": e.method,
                "url": e.url,
                "status": e.status,
                "elapsed_ms": e.elapsed_ms,
                "request_body": e.request_body,
                "response_body": (e.response_body if not isinstance(
                    e.response_body, bytes) else f"<{len(e.response_body)} bytes>"),
                "error": e.error,
            }
            for e in self.exchanges
        ]


def _decode_image(value: Any) -> bytes | None:
    """Accept raw base64 or a data: URL and return PNG/JPEG bytes."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not isinstance(value, str):
        return None
    if value.startswith("data:"):
        _, _, b64 = value.partition(",")
        value = b64
    import base64
    try:
        return base64.b64decode(value, validate=False)
    except Exception:
        return None
