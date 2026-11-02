"""Behavioural trace synthesis.

The hardest part of bypassing a modern CAPTCHA is not reading it — it is
*behaving* like a person while you do. Real sites look at the movement
between pointer events, not just the final answer.

This module produces plausible human-like pointer traces (Fitts's law
timing, eased acceleration, small hand tremor, occasional overshoot and
correction) so that a bot's motion is not a straight line from A to B at
constant speed.

It is also, deliberately, the part of this project that is *most* honest
about its limits: a synthetic trace is still a synthetic trace. It defeats
kinematic checks; it does not defeat attestation. See README, section
"What this does not defeat".
"""
from __future__ import annotations

import math
import random


def _fitts_duration(d: float, w: float = 24.0) -> float:
    """Fitts's law: time to move distance d to a target of width w.

    T = a + b * log2(1 + d / w)
    """
    return 0.08 + 0.12 * math.log2(1.0 + max(0.0, d) / max(1.0, w))


def human_trace(x0: float, y0: float, x1: float, y1: float,
                steps: int | None = None, rng: random.Random | None = None,
                jitter: float = 1.4) -> list[dict]:
    """Generate a human-like pointer path from (x0, y0) to (x1, y1).

    The path is a straight line in *intent* but curved in *execution*:
    a lateral arc (people rarely travel perfectly straight), eased
    velocity (fast in the middle, slow at both ends), and low-amplitude
    tremor on the way.

    Returns a list of events: ``{"x": float, "y": float, "t": float}``
    where ``t`` is the timestamp in seconds since the start of the move.
    """
    rng = rng or random.Random()
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)

    if steps is None:
        # More steps for longer moves, but with a floor so the trace never
        # degenerates into 2 points (which no human browser emits).
        steps = max(8, min(120, int(dist / 6) + 12))

    total_t = _fitts_duration(dist)
    # Overshoot on the final approach for short moves; humans "reach past"
    # and correct. Longer moves get less overshoot.
    overshoot = 0.0
    if dist > 18:
        overshoot = min(9.0, dist * 0.07) * rng.uniform(0.4, 1.0)

    # Lateral arc amplitude, perpendicular to the direction of travel.
    arc = 0.0
    if dist > 25:
        arc = rng.uniform(-1.0, 1.0) * min(14.0, dist * 0.05)

    ux = dx / dist if dist else 0.0
    uy = dy / dist if dist else 0.0
    px, py = -uy, ux          # perpendicular unit vector

    pts: list[dict] = []
    # Timestamps follow the easing curve: velocity is proportional to the
    # derivative of the eased progress (6u(1-u)), so the movement is slow at
    # both ends and fastest mid-flight. The per-step dt is the total
    # duration weighted by that velocity profile, normalised so the whole
    # move takes exactly `total_t` seconds.
    vel = [max(0.05, 6.0 * (i / steps) * (1.0 - i / steps))
           for i in range(steps + 1)]
    vel_sum = sum(vel)
    times = [0.0]
    for i in range(1, steps + 1):
        times.append(times[-1] + total_t * vel[i] / vel_sum)

    for i in range(steps + 1):
        u = i / steps
        # Eased progress: smoothstep gives slow start / slow end.
        eased = u * u * (3.0 - 2.0 * u)

        x = x0 + dx * eased
        y = y0 + dy * eased

        # Arc bows the path sideways, peaking mid-flight.
        x += px * arc * math.sin(math.pi * u)
        y += py * arc * math.sin(math.pi * u)

        # Tremor: small, high-frequency, and it never fully stops.
        x += rng.gauss(0.0, jitter)
        y += rng.gauss(0.0, jitter)

        # Overshoot past the target in the last 12% of the move.
        if overshoot and u > 0.88:
            k = (u - 0.88) / 0.12
            x += ux * overshoot * k
            y += uy * overshoot * k

        pts.append({"x": round(x, 2), "y": round(y, 2),
                    "t": round(times[i], 4)})

    # Land exactly on the target — humans always finish on the pixel.
    pts[-1]["x"] = round(x1, 2)
    pts[-1]["y"] = round(y1, 2)

    # A human occasionally pauses before releasing (correction hesitation).
    if rng.random() < 0.25 and len(pts) > 3:
        pts[-2]["t"] = round(pts[-2]["t"] + rng.uniform(0.02, 0.09), 4)

    return pts


def human_click_trace(x: float, y: float, **kwargs) -> list[dict]:
    """A short trace ending at a click point (approach + settle)."""
    rng = kwargs.pop("rng", None) or random.Random()
    start = {"x": x + rng.uniform(-120, 120), "y": y + rng.uniform(-90, 90)}
    return human_trace(start["x"], start["y"], x, y, rng=rng, **kwargs)


def motion_features(trace: list[dict]) -> dict:
    """Summary statistics of a pointer trace — the signals a server scores.

    Useful for both directions: to check that a synthesised trace looks
    human, and (in the lab) to see what a real human's trace looks like
    so an attacker can match it.
    """
    if len(trace) < 2:
        return {"points": len(trace)}

    pts = [(p["x"], p["y"], p.get("t", 0.0)) for p in trace]
    total_d = 0.0
    speeds = []
    for (x0, y0, t0), (x1, y1, t1) in zip(pts, pts[1:]):
        d = math.hypot(x1 - x0, y1 - y0)
        dt = max(1e-4, t1 - t0)
        total_d += d
        speeds.append(d / dt)

    dt_total = max(1e-4, pts[-1][2] - pts[0][2])
    straight = math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])

    # Direction changes: a straight line has ~0, a jittery path has many.
    angs = []
    for (x0, y0, _), (x1, y1, _) in zip(pts, pts[1:]):
        angs.append(math.atan2(y1 - y0, x1 - x0))
    turns = 0
    for a, b in zip(angs, angs[1:]):
        d = abs((b - a + math.pi) % (2 * math.pi) - math.pi)
        if d > 0.35:
            turns += 1

    return {
        "points": len(pts),
        "duration_s": round(dt_total, 4),
        "path_length_px": round(total_d, 2),
        "straight_line_px": round(straight, 2),
        "efficiency": round(straight / total_d, 3) if total_d else 0.0,
        "mean_speed_px_s": round(sum(speeds) / len(speeds), 2),
        "max_speed_px_s": round(max(speeds), 2),
        "direction_changes": turns,
    }
