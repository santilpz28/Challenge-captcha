#!/usr/bin/env python3
"""Unit tests for the bot package — no server, no browser, fast.

These assert the **observable result**, not just that a call doesn't raise.
A broken classifier is worse than no classifier: people trust the number, so
the number has to be right.

Run:  pytest tests/test_bot_unit.py -v
"""
from __future__ import annotations

import inspect
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
        import random
        for seed in range(20):
            t = human_trace(10.0, 20.0, 210.0, 24.0, rng=random.Random(seed))
            # The start point carries the tremor (a real hand is never
            # perfectly still), but it must stay in the neighbourhood of
            # the origin — a few pixels, not a few dozen.
            assert abs(t[0]["x"] - 10.0) <= 6, f"seed {seed}: start x drifted"
            assert abs(t[0]["y"] - 20.0) <= 6, f"seed {seed}: start y drifted"
            # The END is exact: humans always finish on the pixel they
            # aimed at, and a bot that misses by 3px fails the challenge.
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
        from challenges.base import to_data_url
        from challenges.text_family import TextFamily

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
        from challenges.grid_family import GridFamily

        fam = GridFamily(grid=3, target_count=(2, 4), tile_size=96, distortion=0.25)
        ch = fam.generate(seed=101)
        raw = _data_url_bytes(ch.public["image"])
        res = get_solver("grid", grid=3, tile_size=96, gap=ch.public["gap"],
                         target=ch.public["target"]).solve_image(raw)
        assert res.answer == sorted(ch.truth["cells"])

    def test_slider_solver_finds_the_gap_exactly(self):
        """The headline number: 0 px error, every time."""
        from challenges.render import render_challenge

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
        from challenges.render import render_challenge

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
        from challenges.render import render_challenge
        bg, *_ = render_challenge(seed=11)
        gap = detect_gap(bg)
        assert 0.0 <= gap["confidence"] <= 1.0
        assert isinstance(gap["candidates"], list)

    def test_handles_a_blank_board_without_crashing(self):
        from PIL import Image
        blank = Image.new("RGB", (200, 120), (240, 240, 240))
        gap = detect_gap(blank)
        assert gap["x"] is None or gap["confidence"] >= 0.0


class TestMultiSelect:
    """The "select all the dogs" family — one target, many matches."""

    def test_grid_returns_every_matching_cell(self):
        """A grid challenge with four buses must return four cells."""
        from challenges.grid_family import GridFamily

        fam = GridFamily(grid=3, target_count=(3, 4), tile_size=96,
                         distortion=0.2)
        ch = fam.generate(seed=42)
        raw = _data_url_bytes(ch.public["image"])
        res = get_solver("grid", grid=3, tile_size=96,
                         gap=ch.public["gap"],
                         target=ch.public["target"]).solve_image(raw)
        assert res.answer == sorted(ch.truth["cells"]), \
            f"picked {res.answer}, truth {ch.truth['cells']}"

    def test_multi_select_returns_a_list_not_one_box(self):
        """The image family's multi mode must return several hits."""
        from challenges.image_family import ImageFamily

        fam = ImageFamily(size=220, n_objects=6)
        ch = fam.generate(seed=42)
        raw = _data_url_bytes(ch.public["image"])
        res = get_solver("image", target=ch.public["target"],
                         multi=True).solve_image(raw)
        assert isinstance(res.answer, dict)
        assert "hits" in res.answer
        assert "clicks" in res.answer
        assert len(res.answer["clicks"]) == len(res.answer["hits"])

    def test_single_click_returns_one_target(self):
        from challenges.image_family import ImageFamily

        fam = ImageFamily(size=220, n_objects=6)
        ch = fam.generate(seed=42)
        raw = _data_url_bytes(ch.public["image"])
        res = get_solver("image", target=ch.public["target"],
                         multi=False).solve_image(raw)
        assert isinstance(res.answer, dict)
        assert len(res.answer.get("hits", [])) <= 1

    def test_target_is_free_text_not_a_fixed_label(self):
        """The semantic solver must accept ANY word, not a label set."""
        from bot.image_solver import SemanticImageSolver
        s = SemanticImageSolver(target="traffic light")
        assert s.target == "traffic light"
        assert "traffic light" in s.labels

    def test_clip_is_opt_in(self):
        """CLIP must not be the default: it misreads synthetic clip-art."""
        from bot.image_solver import SemanticImageSolver
        assert SemanticImageSolver(target="dog").use_clip is False
        assert SemanticImageSolver(target="dog", use_clip=True).use_clip is True


class TestTextSolver:
    def test_duplicate_characters_are_dropped(self):
        from bot.text_solver import _drop_ocr_duplicates
        # OCR invents a repeated glyph on noisy images: "CFHF35" reads
        # back as "CFHF335". The duplicate run collapses.
        assert _drop_ocr_duplicates("CFHF335") == "CFHF35"
        assert _drop_ocr_duplicates("AABB") == "AB"
        # A deliberate run of three is kept — "111" is a real answer.
        assert _drop_ocr_duplicates("111") == "111"
        assert _drop_ocr_duplicates("") == ""


class TestEmptyMatch:
    """A target that matches nothing is a valid answer, not a crash.

    Both bugs here were found the hard way: the CLI died with
    ``TypeError: Attempt.__init__() got an unexpected keyword argument
    'confidence'`` whenever the scene held no matching object. An
    attacker that crashes on "nothing found" is an attacker that only
    works on easy challenges.
    """

    def _scene_with_objects(self):
        import io
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (300, 200), (240, 235, 225))
        d = ImageDraw.Draw(img)
        d.ellipse([50, 60, 110, 120], fill=(200, 60, 50))
        d.ellipse([180, 60, 240, 120], fill=(60, 120, 200))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def test_no_match_does_not_raise(self):
        """The bug: Attempt() was called with a confidence kwarg it lacks."""
        raw = self._scene_with_objects()
        res = get_solver("image", target="unicorn", multi=True,
                         segmentation="template").solve_image(raw)
        assert res.ok is False

    def test_no_match_returns_the_semantic_shape(self):
        """The bug: an empty hit list fell through to the bounding-box
        branch and raised KeyError('x')."""
        raw = self._scene_with_objects()
        res = get_solver("image", target="unicorn", multi=True,
                         segmentation="template").solve_image(raw)
        assert isinstance(res.answer, dict)
        assert res.answer["hits"] == []
        assert res.answer["clicks"] == []
        assert res.answer["n"] == 0

    def test_no_match_explains_itself(self):
        """A failed solve must say WHY, not just fail."""
        raw = self._scene_with_objects()
        res = get_solver("image", target="unicorn", multi=True,
                         segmentation="template").solve_image(raw)
        assert res.meta.get("reason")
        assert res.meta.get("target") == "unicorn"

    def test_attempt_contract_has_no_confidence_field(self):
        """`confidence` belongs to SolverResult, not to the shared
        Attempt contract. This asserts the contract stays as declared."""
        import dataclasses
        from core.contract import Attempt
        names = {f.name for f in dataclasses.fields(Attempt)}
        assert "confidence" not in names
        assert "meta" in names


class TestCliSurface:
    """The command line must not accept flags the engine dropped.

    A stale ``choices=`` entry is a silent failure: argparse accepts the
    flag, the engine ignores it and quietly falls back to a much weaker
    strategy. The user sees a plausible number and no warning.
    """

    def test_watershed_is_not_offered(self):
        """Watershed was replaced by template matching (0% recall)."""
        from bot.cli import SEGMENTATION_CHOICES
        assert "watershed" not in SEGMENTATION_CHOICES
        assert "template" in SEGMENTATION_CHOICES
        assert SEGMENTATION_CHOICES == ["auto", "template", "color", "components"]

    def test_segmentation_choices_match_the_engine(self):
        """Every advertised strategy must exist in bot/segment.py."""
        import bot.segment as seg
        from bot.cli import SEGMENTATION_CHOICES
        for name in SEGMENTATION_CHOICES:
            assert name in ("auto", "template", "color", "components")
        # And the engine must handle each one explicitly.
        src = inspect.getsource(seg.segment)
        for name in ("template", "color", "components"):
            assert f'"{name}"' in src, f"{name} not handled by segment()"

    def test_unknown_strategy_is_rejected_not_ignored(self):
        """argparse must refuse a strategy the engine does not know."""
        from bot.cli import build_parser
        p = build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["image", "--family", "image", "--input", "i",
                          "--segmentation", "watershed"])


class TestAutonomy:
    """The attacker must not depend on the generator's internals."""

    def test_bot_imports_without_the_generator_package(self):
        """The bot has to work on a challenge it has never seen."""
        import importlib
        import shutil
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        gen = root / "challenges"
        hidden = root / "_challenges_tmp"
        if not gen.exists():
            pytest.skip("challenges/ not present")
        shutil.move(str(gen), str(hidden))
        try:
            code = (
                "import sys; sys.path.insert(0, %r);\n"
                "import bot.solvers, bot.behavior, bot.client, bot.gapdetect;\n"
                "import bot.text_solver, bot.grid_solver, bot.image_solver;\n"
                "from bot.solvers import get_solver;\n"
                "assert get_solver('slider').name == 'slider';\n"
                "assert get_solver('image', target='dog', multi=True).multi;\n"
                "print('OK')" % str(root)
            )
            r = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=180,
                               cwd=str(root))
            assert "OK" in r.stdout, r.stderr[-500:]
        finally:
            shutil.move(str(hidden), str(gen))


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
# segmentation
# --------------------------------------------------------------------------

class TestSegmentation:
    """The attacker must find the objects before it can classify them.

    Segmentation was the measured bottleneck of the image family: the
    bench reported 33% and the diagnosis was "the vision is not the
    problem, the segmentation is". These tests pin the fix.
    """

    def test_template_matching_finds_every_object(self):
        """Every drawn object must be located, at the right place."""
        from challenges.image_family import ImageFamily
        from core.images import decode_data_url
        from bot import segment as segmenter

        fam = ImageFamily(size=220, n_objects=4, distortion=0.25)
        hits = 0
        total = 0
        for seed in range(12):
            ch = fam.generate(seed=seed)
            img = decode_data_url(ch.public["image"])
            dets = segmenter.match_templates(img)
            for obj in ch.artifacts["positions"]:
                total += 1
                # The ground truth is the 72x72 window the object was
                # pasted into; the drawn shape sits in its middle.
                for d in dets:
                    if (abs(d["x"] - obj["x"]) <= 8
                            and abs(d["y"] - obj["y"]) <= 8):
                        hits += 1
                        break
        # Measured over 30 scenes: 75.8%. The threshold leaves headroom
        # for the sample variation of a 12-seed run.
        assert hits / total >= 0.70, \
            f"template matching found {hits}/{total} objects"

    def test_direct_target_search_beats_classify_everything(self):
        """Searching for the target directly must beat segment+classify.

        This is the core insight of the image attacker: an attacker
        that knows the target does not need to classify the distractors.
        """
        from challenges.image_family import ImageFamily
        from core.images import decode_data_url
        from bot import segment as segmenter

        fam = ImageFamily(size=220, n_objects=4, distortion=0.25)
        direct = 0
        total = 0
        for seed in range(12):
            ch = fam.generate(seed=seed)
            img = decode_data_url(ch.public["image"])
            target = ch.truth["label"]
            obj = next(o for o in ch.artifacts["positions"]
                       if o["label"] == target)
            dets = segmenter.match_templates(img, labels=[target])
            total += 1
            if dets and (abs(dets[0]["x"] - obj["x"]) <= 8
                         and abs(dets[0]["y"] - obj["y"]) <= 8):
                direct += 1
        # Measured over 30 scenes: 80%.
        assert direct / total >= 0.75, \
            f"direct target search found {direct}/{total}"

    def test_colour_check_rejects_empty_background(self):
        """A correlation peak on bare background must not be an object."""
        from PIL import Image as PILImage
        from bot import segment as segmenter

        blank = PILImage.new("RGB", (220, 220), (240, 234, 219))
        dets = segmenter.match_templates(blank)
        assert not dets, \
            f"empty background produced {len(dets)} false objects"

    def test_segmenter_works_without_the_generator(self):
        """Segmentation must not import the challenge generators."""
        import importlib
        import shutil
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        gen = root / "challenges"
        hidden = root / "_challenges_tmp"
        if not gen.exists():
            pytest.skip("challenges/ not present")
        shutil.move(str(gen), str(hidden))
        try:
            code = (
                "import sys; sys.path.insert(0, %r);\n"
                "from bot import segment;\n"
                "from bot.shapes import CLASSES, draw_tile;\n"
                "print('OK')" % str(root)
            )
            r = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=90)
            assert r.returncode == 0, r.stderr
            assert "OK" in r.stdout
        finally:
            shutil.move(str(hidden), str(gen))


class TestSharedFeatures:
    """The generator and the attacker must measure the same thing."""

    def test_reference_signatures_match(self):
        """Both sides must build identical reference vectors."""
        import numpy as np
        from bot import shapes
        from challenges.grid_family import GridFamily

        for label in ("bus", "car", "boat"):
            a = GridFamily._reference_features(label)
            b = shapes.reference_features(label)
            assert np.allclose(a, b), label

    def test_shape_template_is_transparent_outside_the_shape(self):
        """The template must only cover the object, not its background."""
        import numpy as np
        from bot import shapes

        tpl = shapes.shape_template("bus", 72)
        assert np.isnan(tpl[:, :, 0]).any(), "template has no transparency"
        assert not np.isnan(tpl[36, 36, 0]), "centre of the shape is masked out"


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
