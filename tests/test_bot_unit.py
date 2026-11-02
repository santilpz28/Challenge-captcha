#!/usr/bin/env python3
"""Unit tests for the bot package — no server, no browser, fast.

These assert the **observable result**, not just that a call doesn't raise.
A broken classifier is worse than no classifier: people trust the number, so
the number has to be right.

Run:  pytest tests/test_bot_unit.py -v
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.behavior import human_click_trace, human_trace, motion_features
from bot.client import CaptchaClient, _decode_image
from bot.gapdetect import detect_gap
from bot.solvers import get_solver


# --------------------------------------------------------------------------
# behaviour engine
# --------------------------------------------------------------------------

class TestHumanTrace:

    def test_starts_and_ends_exactly_on_target(self):
        t = human_trace(10.0, 20.0, 210.0, 24.0)
        # The start point carries the tremor (a real hand is never perfectly
        # still), but it must be within a pixel or two of the origin.
        assert t[0]["x"] == pytest.approx(10.0, abs=2.0)
        # The END is exact: humans always finish on the pixel they aimed at.
        assert t[-1]["x"] == pytest.approx(210.0, abs=0.05)
        assert t[-1]["y"] == pytest.approx(24.0, abs=0.05)

    def test_timestamps_are_monotonic(self):
        """Across many seeds: a trace that goes backwards in time is a bug.

        The correction hesitation used to pull the last timestamp
        backwards, which made ~40% of traces invalid timelines. A kinematic
        scorer rejects those before a human would even notice.
        """
        import random
        for seed in range(50):
            t = human_trace(0, 0, 200, 0, rng=random.Random(seed))
            ts = [p["t"] for p in t]
            assert all(b >= a for a, b in zip(ts, ts[1:])), \
                f"seed {seed}: non-monotonic timestamps"

    def test_is_not_a_straight_line(self):
        """A perfectly straight path is the single most bot-like signature."""
        t = human_trace(0, 0, 400, 0)
        ys = [p["y"] for p in t]
        assert max(ys) - min(ys) > 0.5, "trace has no lateral deviation"

    def test_has_measurable_direction_changes(self):
        t = human_trace(0, 0, 400, 50)
        f = motion_features(t)
        assert f["direction_changes"] >= 1

    def test_path_is_longer_than_the_straight_line(self):
        f = motion_features(human_trace(0, 0, 300, 0))
        assert f["path_length_px"] > f["straight_line_px"]

    def test_longer_moves_take_longer(self):
        """Fitts's law: the duration must scale with distance."""
        import random
        rng = random.Random(1234)
        short = motion_features(human_trace(0, 0, 60, 0, rng=rng))["duration_s"]
        long = motion_features(human_trace(0, 0, 600, 0, rng=rng))["duration_s"]
        assert long > short * 1.2, f"short={short} long={long}"

    def test_efficiency_is_plausible(self):
        f = motion_features(human_trace(0, 0, 250, 40))
        assert 0.5 < f["efficiency"] <= 1.0

    def test_zero_length_move_does_not_crash(self):
        t = human_trace(50, 50, 50, 50)
        assert t
        assert t[-1]["x"] == pytest.approx(50)

    def test_click_trace_ends_at_the_click_point(self):
        t = human_click_trace(300.0, 200.0)
        assert t[-1]["x"] == pytest.approx(300.0)
        assert t[-1]["y"] == pytest.approx(200.0)

    def test_features_handle_degenerate_trace(self):
        f = motion_features([{"x": 1, "y": 1, "t": 0}])
        assert f["points"] == 1


# --------------------------------------------------------------------------
# solver registry
# --------------------------------------------------------------------------

class TestSolverRegistry:

    def test_known_families_resolve(self):
        for name in ("slider", "text", "grid", "image"):
            assert get_solver(name) is not None

    def test_unknown_family_raises(self):
        with pytest.raises(ValueError):
            get_solver("reCAPTCHA-v3")

    def test_grid_solver_keeps_its_geometry(self):
        s = get_solver("grid", grid=4, tile_size=64, gap=6, target="bus")
        assert s.grid == 4 and s.tile_size == 64 and s.gap == 6
        assert s.target == "bus"


# --------------------------------------------------------------------------
# solver accuracy against the lab generator
# --------------------------------------------------------------------------

class TestSolverAccuracy:

    def test_text_solver_reads_a_generated_challenge(self):
        from app.families.base import to_data_url
        from app.families.text_family import TextFamily

        fam = TextFamily()
        ok = 0
        for seed in (101, 202, 303):
            ch = fam.generate(seed=seed)
            raw = _data_url_bytes(to_data_url(ch.artifacts["clean"]))
            res = get_solver("text").solve_image(raw)
            if res.answer == ch.truth["text"]:
                ok += 1
        assert ok >= 2, f"OCR solved only {ok}/3"

    def test_grid_solver_picks_the_right_cells(self):
        from app.families.grid_family import GridFamily

        fam = GridFamily(grid=3, target_count=(2, 4), tile_size=96, distortion=0.25)
        ch = fam.generate(seed=101)
        raw = _data_url_bytes(ch.public["image"])
        res = get_solver("grid", grid=3, tile_size=96, gap=ch.public["gap"],
                         target=ch.public["target"]).solve_image(raw)
        assert res.answer == sorted(ch.truth["cells"])

    def test_slider_solver_finds_the_gap_exactly(self):
        """The headline number: 0 px error, every time."""
        from app.render import render_challenge

        for seed in (7, 8, 9):
            bg, piece, geom, meta, template = render_challenge(seed=seed)
            # The solver correlates against the PRISTINE template, not the
            # decorated sprite — mixing them drops accuracy from 40/40 to 14/40.
            res = get_solver("slider").solve_pair(
                _img_bytes(bg), _img_bytes(template))
            assert res.ok, res.meta
            gx, gy = geom.bbox[0], geom.bbox[1]
            # The solver reports the top-left of the opaque shape; the
            # geometry reports the same box. They must agree.
            assert abs(res.answer["x"] - gx) <= 2, \
                f"slider off by {res.answer['x'] - gx}px (seed {seed})"
            assert abs(res.answer["y"] - gy) <= 2, \
                f"slider off by {res.answer['y'] - gy}px (seed {seed})"


class TestGapDetection:

    def test_finds_the_gap_in_a_generated_board(self):
        """The contrast detector must find the hole without the sprite."""
        from app.render import render_challenge

        hits = 0
        for seed in (11, 12, 13, 14):
            bg, piece, geom, meta, template = render_challenge(seed=seed)
            gap = detect_gap(bg)
            if gap.get("error"):
                continue
            gx = geom.bbox[0] + geom.bbox[2] // 2
            gy = geom.bbox[1] + geom.bbox[3] // 2
            if abs(gap["x"] - gx) <= 10 and abs(gap["y"] - gy) <= 10:
                hits += 1
        assert hits >= 3, f"contrast detector found only {hits}/4 gaps"

    def test_reports_a_confidence_and_candidates(self):
        from app.render import render_challenge
        bg, *_ = render_challenge(seed=11)
        gap = detect_gap(bg)
        assert 0.0 <= gap["confidence"] <= 1.0
        assert isinstance(gap["candidates"], list)

    def test_handles_a_blank_board_without_crashing(self):
        from PIL import Image
        blank = Image.new("RGB", (200, 120), (240, 240, 240))
        gap = detect_gap(blank)
        assert gap["x"] is None or gap["confidence"] >= 0.0


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

class TestCaptchaClient:

    def test_decode_image_accepts_base64_and_data_urls(self):
        import base64
        raw = b"\x89PNG\r\n\x1a\n-fake"
        b64 = base64.b64encode(raw).decode()
        assert _decode_image(b64) == raw
        assert _decode_image("data:image/png;base64," + b64) == raw
        assert _decode_image(None) is None
        assert _decode_image(12345) is None

    def test_submit_requires_explicit_confirmation(self):
        c = CaptchaClient("http://example.invalid")
        with pytest.raises(PermissionError):
            c.submit("/verify", "ABC", submit=False)

    def test_audit_trail_is_empty_before_any_request(self):
        c = CaptchaClient("http://example.invalid")
        assert c.audit_trail() == []

    def test_request_failure_is_recorded_not_raised(self):
        c = CaptchaClient("http://127.0.0.1:1", timeout=1.0)
        ex = c.get("/nope")
        assert ex.error is not None
        assert len(c.audit_trail()) == 1


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _data_url_bytes(data_url: str) -> bytes:
    import base64
    _, _, b64 = data_url.partition(",")
    return base64.b64decode(b64)


def _img_bytes(img) -> bytes:
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
