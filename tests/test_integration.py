#!/usr/bin/env python3
"""End-to-end integration test: the bot attacks a live CAPTCHA server.

This is the test that proves the whole chain works, not just the pieces:

    fetch challenge -> solve -> synthesise human trace -> submit -> accepted

The server is NOT bundled with this repo — it points at any endpoint that
speaks the challenge/verify protocol, which is exactly how the bot is
used in the field. By default it targets a locally running instance of
the companion lab (``captcha-lab``); override it with:

    CAPTCHA_TEST_URL=http://host:port python tests/test_integration.py

Run:
    python tests/test_integration.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = os.environ.get("CAPTCHA_TEST_URL", "http://127.0.0.1:8099").rstrip("/")
CHALLENGE_PATH = os.environ.get("CAPTCHA_TEST_CHALLENGE", "/api/challenge")
VERIFY_PATH = os.environ.get("CAPTCHA_TEST_VERIFY", "/api/verify")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def reachable(url: str, timeout: float = 3.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except urllib.error.HTTPError:
            return True          # the server answered, that's what matters
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    print(f"[i] target: {BASE}{CHALLENGE_PATH}")

    health = f"{BASE}/api/health"
    if not reachable(health):
        print(f"[-] no CAPTCHA server at {BASE}")
        print("    start one, e.g.:")
        print("      python -m uvicorn app.server:app --port 8099   # in captcha-lab")
        print("    or point the test elsewhere:")
        print("      CAPTCHA_TEST_URL=https://target.example python tests/test_integration.py")
        return 2

    from bot.behavior import human_trace
    from bot.client import CaptchaClient
    from bot.solvers import get_solver

    # ------------------------------------------------------------------
    print("\n[slider] attack the live challenge")
    client = CaptchaClient(BASE)
    ch = client.fetch_challenge(CHALLENGE_PATH,
                                image_field="background",
                                piece_field="piece")
    check("challenge fetched with images",
          ch.image is not None and ch.piece is not None,
          f"bg={len(ch.image or b'')}B piece={len(ch.piece or b'')}B")
    check("challenge id present", bool(ch.challenge_id))

    res = get_solver("slider").solve_pair(ch.image, ch.piece,
                                          sprite=ch.raw.get("sprite"))
    check("solver produced an answer", res.ok and res.answer,
          f"{res.answer} strategy={res.meta.get('strategy')}")

    truth = ch.raw.get("shape_centre")
    if truth:
        dx = abs(res.answer["x"] - truth["x"])
        dy = abs(res.answer["y"] - truth["y"])
        check("answer within 6px of the gap centre", dx <= 6 and dy <= 6,
              f"d=({dx},{dy})")

    trace = human_trace(12, 120, float(res.answer["x"]), float(res.answer["y"]))
    check("synthesised trace has enough points", len(trace) >= 20,
          f"{len(trace)} points")

    ex = client.submit(VERIFY_PATH,
                       {"x": res.answer["x"], "y": res.answer["y"]},
                       challenge_id=ch.challenge_id,
                       extra={"trace": trace,
                              "pointer_events": len(trace),
                              "duration_ms": round(trace[-1]["t"] * 1000, 1)},
                       submit=True)
    body = ex.response_body
    if isinstance(body, str):
        body = json.loads(body)
    check("server accepted the solve", bool(body.get("ok")),
          f"distance_px={body.get('distance_px')} reason={body.get('reason')}")

    # ------------------------------------------------------------------
    print("\n[replay] a solved challenge cannot be replayed")
    ex2 = client.submit(VERIFY_PATH,
                        {"x": res.answer["x"], "y": res.answer["y"]},
                        challenge_id=ch.challenge_id,
                        extra={"trace": trace, "pointer_events": len(trace)},
                        submit=True)
    check("replay is rejected", ex2.status in (404, 409),
          f"HTTP {ex2.status}")

    # ------------------------------------------------------------------
    print("\n[cli] the command line entrypoint works")
    cli = subprocess.run(
        [sys.executable, "-m", "bot.cli", "--version"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    check("cli reports its version",
          cli.returncode == 0 and "1.0.0" in cli.stdout,
          cli.stdout.strip() or cli.stderr.strip()[:80])

    cli2 = subprocess.run(
        [sys.executable, "-m", "bot.cli", "api", "--url", BASE,
         "--path", CHALLENGE_PATH, "--family", "slider", "--submit"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    check("cli api mode solves and submits",
          cli2.returncode == 0 and '"ok": true' in cli2.stdout,
          (cli2.stdout or cli2.stderr)[-160:].replace("\n", " "))

    print("\n" + "=" * 70)
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
