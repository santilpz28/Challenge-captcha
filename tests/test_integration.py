#!/usr/bin/env python3
"""End-to-end integration test: the bot attacks the lab server for real.

This is the test that proves the whole chain works, not just the pieces:

    fetch challenge -> solve -> synthesise human trace -> submit -> accepted

It boots the lab server itself (on a free port), so it needs nothing
preinstalled. Run:

    python3 tests/test_integration.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(url: str, timeout: float = 25.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    port = free_port()
    env = dict(os.environ, LAB_MODE="true", PYTHONPATH=str(ROOT))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    base = f"http://127.0.0.1:{port}"
    try:
        if not wait_for(f"{base}/api/health"):
            print("[-] lab server did not come up")
            return 2

        from bot.behavior import human_trace
        from bot.client import CaptchaClient
        from bot.solvers import get_solver

        # ------------------------------------------------------------------
        print("\n[slider] attack the live challenge")
        client = CaptchaClient(base)
        ch = client.fetch_challenge("/api/challenge",
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

        ex = client.submit("/api/verify",
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
        check("server accepted the behaviour",
              body.get("behaviour", {}).get("human_score", 0) > 0.5,
              f"score={body.get('behaviour', {}).get('human_score')}")

        # ------------------------------------------------------------------
        print("\n[replay] a solved challenge cannot be replayed")
        ex2 = client.submit("/api/verify",
                            {"x": res.answer["x"], "y": res.answer["y"]},
                            challenge_id=ch.challenge_id,
                            extra={"trace": trace, "pointer_events": len(trace)},
                            submit=True)
        check("replay is rejected", ex2.status == 404, f"HTTP {ex2.status}")

        # ------------------------------------------------------------------
        print("\n[sessions] the agent token attributes the solve to AI")
        sess = client.get("/api/session?kind=ai_agent")
        token = sess.response_body.get("token")
        check("agent session issued", bool(token) and token.startswith("lab_ai_agent."),
              str(token)[:24] if token else "none")

        client.headers["Authorization"] = f"Bearer {token}"
        ch2 = client.fetch_challenge("/api/challenge",
                                     image_field="background",
                                     piece_field="piece")
        res2 = get_solver("slider").solve_pair(ch2.image, ch2.piece,
                                               sprite=ch2.raw.get("sprite"))
        ex3 = client.submit("/api/verify",
                            {"x": res2.answer["x"], "y": res2.answer["y"]},
                            challenge_id=ch2.challenge_id,
                            extra={"trace": trace, "pointer_events": len(trace)},
                            submit=True)
        body3 = ex3.response_body
        if isinstance(body3, str):
            body3 = json.loads(body3)
        check("solve attributed to AI AGENT",
              body3.get("solved_by") == "ai_agent",
              f"solved_by={body3.get('solved_by')}")

        # ------------------------------------------------------------------
        print("\n[cli] the command line entrypoint works")
        cli = subprocess.run(
            [sys.executable, "-m", "bot.cli", "--version"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        check("cli reports its version", cli.returncode == 0 and "1.0.0" in cli.stdout,
              cli.stdout.strip() or cli.stderr.strip()[:80])

        cli2 = subprocess.run(
            [sys.executable, "-m", "bot.cli", "api", "--url", base,
             "--path", "/api/challenge", "--family", "slider", "--submit"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        check("cli api mode solves and submits",
              cli2.returncode == 0 and '"ok": true' in cli2.stdout,
              (cli2.stdout or cli2.stderr)[-160:].replace("\n", " "))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    print("\n" + "=" * 70)
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
