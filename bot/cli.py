#!/usr/bin/env python3
"""Challenge Captcha Bot — command line entrypoint.

Examples
--------
Solve a CAPTCHA image from disk::

    python3 -m bot.cli image --family text --input captcha.png

Solve a slider given the board and the piece sprite::

    python3 -m bot.cli image --family slider --input board.png --piece piece.png

Beat a live endpoint that exposes its challenge as JSON::

    python3 -m bot.cli api --url https://site.tld/api/captcha \\
        --family text --submit --out report.json

Drive a real page in a real browser::

    python3 -m bot.cli browser --url https://site.tld/login \\
        --family slider --image-selector '#board' --piece-selector '#piece'

Benchmark every family against the lab generator (the numbers in the README)::

    python3 -m bot.cli bench --runs 30
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import __version__

# Segmentation strategies exposed on the command line. They mirror the
# strategies in bot/segment.py: "template" is the one that separates
# touching objects, "auto" prefers it when the shape palette fits.
# Keep this list in sync with segment(), or a stale choice silently
# falls back to colour clustering.
SEGMENTATION_CHOICES = ["auto", "template", "color", "components"]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _out(data: dict, path: str | None) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if path:
        Path(path).write_text(text, encoding="utf-8")
        print(f"[+] report written to {path}")
    else:
        print(text)


def _read_image(path: str) -> bytes:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[-] file not found: {path}")
    return p.read_bytes()


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

def cmd_image(args) -> int:
    """Solve a CAPTCHA image from disk."""
    from bot.solvers import get_solver

    data = _read_image(args.input)
    kwargs = {}
    if args.family == "grid":
        kwargs = {"grid": args.grid, "tile_size": args.tile_size,
                  "gap": args.gap, "target": args.target}
    elif args.family == "image":
        kwargs = {"target": args.target, "multi": args.multi,
                  "threshold": args.threshold,
                  "use_clip": bool(getattr(args, "clip", False)),
                  "segmentation": getattr(args, "segmentation", "auto")}
    solver = get_solver(args.family, **kwargs)

    if args.family == "slider":
        if not args.piece:
            raise SystemExit("[-] slider needs --piece (the piece sprite)")
        res = solver.solve_pair(data, _read_image(args.piece),
                                strategy=args.strategy)
    else:
        res = solver.solve_image(data)

    _out(res.to_dict(), args.out)
    return 0 if res.ok else 1


def cmd_api(args) -> int:
    """Solve a challenge exposed by a live JSON endpoint."""
    from bot.client import CaptchaClient
    from bot.solvers import get_solver

    client = CaptchaClient(args.url, timeout=args.timeout,
                            headers={"Authorization": args.token}
                            if args.token else None)

    challenge = client.fetch_challenge(
        args.path, image_field=args.image_field,
        piece_field=args.piece_field if args.family == "slider" else None)

    if not challenge.image:
        _out({"error": "no image in challenge payload",
              "payload_keys": list(challenge.raw.keys())}, args.out)
        return 2

    solver = get_solver(args.family, grid=args.grid, tile_size=args.tile_size,
                        gap=args.gap, target=args.target,
                        multi=bool(getattr(args, "multi", False)),
                        threshold=float(getattr(args, "threshold", 0.22)),
                        use_clip=bool(getattr(args, "clip", False)),
                        segmentation=getattr(args, "segmentation", "auto"))
    if args.family == "slider":
        if not challenge.piece:
            _out({"error": "slider challenge carried no piece sprite"},
                 args.out)
            return 2
        res = solver.solve_pair(challenge.image, challenge.piece,
                                strategy=args.strategy,
                                sprite=challenge.raw.get("sprite"))
    else:
        res = solver.solve_image(challenge.image, target=args.target)

    report = {"solved_answer": res.to_dict(),
              "challenge_id": challenge.challenge_id,
              "audit_trail": client.audit_trail()}

    if args.submit:
        # Send a human-like pointer trace alongside the answer. A site that
        # scores the trajectory would reject a bare coordinate pair; and a
        # site that does NOT score it is exactly the vulnerability the lab
        # measures.
        trace = []
        if args.family == "slider" and isinstance(res.answer, dict):
            from bot.behavior import human_trace
            x0 = float(args.trace_from or 12)
            y0 = float(args.trace_from_y or 120)
            trace = human_trace(x0, y0,
                                float(res.answer.get("x", 0)),
                                float(res.answer.get("y", 0)))
        elif args.family == "image" and getattr(args, "multi", False):
            # Multi-select: one human-like approach per matched object.
            from bot.behavior import human_click_trace
            clicks = (res.answer.get("clicks", [])
                      if isinstance(res.answer, dict) else [])
            for cx, cy in clicks:
                trace.extend(human_click_trace(12, 120,
                                               float(cx), float(cy)))
        elif args.family in ("grid", "image"):
            from bot.behavior import human_trace
            trace = human_trace(12, 120, 180, 120)

        extra = {"trace": trace, "pointer_events": len(trace),
                 "duration_ms": (round(trace[-1]["t"] * 1000, 1)
                                 if trace else 0)}
        if args.family == "image" and getattr(args, "multi", False):
            # Multi-select sites expect the list of chosen points.
            extra["clicks"] = (res.answer.get("clicks", [])
                               if isinstance(res.answer, dict) else [])
        ex = client.submit(args.submit_path, res.answer,
                           challenge_id=challenge.challenge_id,
                           extra=extra, submit=True)
        report["submission"] = {
            "status": ex.status,
            "response": ex.response_body,
            "error": ex.error,
            "trace_points": len(trace),
        }
        report["audit_trail"] = client.audit_trail()

    _out(report, args.out)
    return 0 if res.ok else 1


def cmd_browser(args) -> int:
    """Drive a real browser against a real page."""
    from bot.browser import BrowserSolver

    solver = BrowserSolver(headless=not args.headed,
                           timeout_ms=args.timeout * 1000)
    res = solver.solve(
        args.url, family=args.family,
        image_selector=args.image_selector,
        piece_selector=args.piece_selector,
        input_selector=args.input_selector,
        submit_selector=args.submit_selector,
        target=args.target, grid=args.grid,
        tile_size=args.tile_size, gap=args.gap,
        screenshot_out=args.screenshot)

    _out(res.to_dict(), args.out)
    return 0 if res.solved else 1


def cmd_bench(args) -> int:
    """Run the multi-family benchmark."""
    sys.argv = [sys.argv[0], "--runs", str(args.runs)]
    if args.families:
        sys.argv += ["--families", args.families]
    if args.json:
        sys.argv += ["--json", args.json]
    if args.clip:
        sys.argv += ["--clip"]
    import bench
    bench.main()
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bot",
        description="Challenge Captcha Bot — automated CAPTCHA solving "
                    "(authorized testing only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Use only against systems you own or are authorised to test.")
    p.add_argument("--version", action="version",
                   version=f"challenge-captcha-bot {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- image ----
    im = sub.add_parser("image", help="solve a CAPTCHA image from disk")
    im.add_argument("--family", required=True,
                    choices=["slider", "text", "grid", "image"])
    im.add_argument("--input", required=True, help="path to the CAPTCHA image")
    im.add_argument("--piece", help="path to the piece sprite (slider only)")
    im.add_argument("--target", help="target class to look for (grid/image)")
    im.add_argument("--grid", type=int, default=3)
    im.add_argument("--tile-size", type=int, default=96)
    im.add_argument("--gap", type=int, default=8)
    im.add_argument("--segmentation", default="auto",
                    choices=SEGMENTATION_CHOICES,
                    help="segmentation strategy for the image family")
    im.add_argument("--strategy", default="auto",
                    choices=["auto", "geometry", "template", "contrast"],
                    help="slider strategy: geometry reads the position from "
                         "the payload, template correlates the sprite, "
                         "contrast finds the gap by local contrast")
    im.add_argument("--out", help="write the JSON report here")
    im.set_defaults(func=cmd_image)

    # ---- api ----
    ap = sub.add_parser("api", help="solve a challenge from a JSON endpoint")
    ap.add_argument("--url", required=True, help="base URL of the site")
    ap.add_argument("--path", default="/api/captcha",
                    help="challenge endpoint path (default: /api/captcha)")
    ap.add_argument("--submit-path", default="/api/verify",
                    help="answer endpoint path (default: /api/verify)")
    ap.add_argument("--family", required=True,
                    choices=["slider", "text", "grid", "image"])
    ap.add_argument("--token", help="Authorization header value")
    ap.add_argument("--image-field", default="background",
                    help="JSON key holding the image (default: background)")
    ap.add_argument("--piece-field", default="piece",
                    help="JSON key holding the piece sprite (slider only)")
    ap.add_argument("--target", help="target class to look for (grid/image)")
    ap.add_argument("--multi", action="store_true",
                    help="select EVERY matching object (image family)")
    ap.add_argument("--threshold", type=float, default=0.22,
                    help="minimum confidence for a match")
    ap.add_argument("--clip", action="store_true",
                    help="use the CLIP semantic attacker (needs the ONNX model)")
    ap.add_argument("--grid", type=int, default=3)
    ap.add_argument("--tile-size", type=int, default=96)
    ap.add_argument("--gap", type=int, default=8)
    ap.add_argument("--segmentation", default="auto",
                    choices=SEGMENTATION_CHOICES,
                    help="segmentation strategy for the image family")
    ap.add_argument("--strategy", default="auto",
                    choices=["auto", "geometry", "template", "contrast"])
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--trace-from", type=float, default=12.0,
                    help="x the synthetic drag starts from")
    ap.add_argument("--trace-from-y", type=float, default=120.0,
                    help="y the synthetic drag starts from")
    ap.add_argument("--submit", action="store_true",
                    help="actually POST the answer (write operation)")
    ap.add_argument("--out", help="write the JSON report here")
    ap.set_defaults(func=cmd_api)

    # ---- browser ----
    br = sub.add_parser("browser", help="drive a real page in a real browser")
    br.add_argument("--url", required=True)
    br.add_argument("--family", required=True,
                    choices=["slider", "text", "grid", "image"])
    br.add_argument("--image-selector", help="CSS selector for the CAPTCHA image")
    br.add_argument("--piece-selector", help="CSS selector for the piece sprite")
    br.add_argument("--input-selector", help="CSS selector for the text input")
    br.add_argument("--submit-selector", help="CSS selector for the submit button")
    br.add_argument("--target", help="target class to look for (grid/image)")
    br.add_argument("--grid", type=int, default=3)
    br.add_argument("--tile-size", type=int, default=96)
    br.add_argument("--gap", type=int, default=8)
    br.add_argument("--timeout", type=float, default=20.0)
    br.add_argument("--headed", action="store_true", help="show the browser")
    br.add_argument("--screenshot", help="save a screenshot here")
    br.add_argument("--out", help="write the JSON report here")
    br.set_defaults(func=cmd_browser)

    # ---- bench ----
    be = sub.add_parser("bench", help="run the multi-family benchmark")
    be.add_argument("--runs", type=int, default=30)
    be.add_argument("--families", help="comma separated: text,grid,image")
    be.add_argument("--json", help="write the raw results here")
    be.add_argument("--clip", action="store_true",
                    help="include the CLIP attacker (slow on CPU)")
    be.set_defaults(func=cmd_bench)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PermissionError as e:
        print(f"[-] {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
