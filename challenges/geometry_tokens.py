"""Canvas and renderer defaults for the challenge generator.

Single source of truth for the geometry the generators and the verifier
share. The solver does NOT import these — it must work on a challenge
whose parameters it has never seen.
"""

# --- canvas / renderer defaults ---------------------------------------------
CANVAS_W = 320
CANVAS_H = 160
PIECE_SIZE = 46          # slider puzzle piece edge (px)
PIECE_MARGIN_X = 8       # keep piece fully inside the canvas
TRACK_TOLERANCE = 6      # px tolerance around the true gap x
TRACK_TOLERANCE_Y = 8    # px tolerance around the true gap y
PIECE_JITTER = 7         # visual noise offset applied to the rendered gap
MIN_DRAG_DISTANCE = 12   # below this a drag is a click, not a solve attempt


def hexrgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
