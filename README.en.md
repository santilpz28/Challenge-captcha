# Challenge Captcha Bot

> **A CAPTCHA is only as strong as its weakest attacker.**
> This repo **is** the attacker.

[ES](README.md) · [EN](README.en.md)

Automated CAPTCHA solving bot with **four local attackers** (slider, OCR text,
multi-select grid, single image), a **human-like movement engine** for pointer
traces, an HTTP client for endpoints that expose their challenge as JSON, and a
**real browser** mode (Playwright) for canvas widgets.

Everything runs **locally**. No cloud OCR, no API keys, no target-specific
training: the attacker has never seen the site's font.

> ⚖️ **Authorized use only.** This tool exists for security auditing: measuring
> whether *your* CAPTCHA holds up, or a client's under a signed contract. Using
> it for spam, bulk registration or fraud is out of scope and illegal in most
> jurisdictions. See [Licence and use](#licence-and-use).

---

## What's here

**This is the attacker only.** No server, no web UI, no challenge generator:
it is a tool you point at whatever CAPTCHA you are facing.

| Component | What it does |
|---|---|
| `bot/solvers.py` | The 4 attackers: slider (3 strategies), text (multi-binarisation OCR), grid (Hu moments), image (k-means + shapes) |
| `bot/image_solver.py` | **Semantic** attacker: segments the scene and classifies each object with CLIP → "click the dog", "all the dogs" |
| `bot/grid_solver.py` | Grid attacker: classifies each cell and selects every match |
| `bot/text_solver.py` | OCR with multi-binarisation voting + duplicate cleanup |
| `bot/slider_solver.py` | Masked normalised cross-correlation over FFT |
| `bot/gapdetect.py` | Gap detection by local contrast — the strategy that needs no sprite |
| `bot/behavior.py` | Human trace engine: Fitts's law, easing, tremor, overshoot |
| `bot/client.py` | HTTP client for JSON-exposed challenges, with an audit trail |
| `bot/browser.py` | Real-browser mode (Playwright/Chromium): solves and clicks like a person |
| `bot/cli.py` | Unified CLI: `image` · `api` · `browser` · `bench` |
| `core/` | The shared contract (`Challenge`/`Attempt`) — the only thing the attacker and the challenge have in common |
| `challenges/` | Auxiliary challenge **generators**, only for tests and the bench. The bot does **not** need them: it works without them |

### The attacker is autonomous

The design stops the bot cheating: it **imports nothing from the generator**
except the contract (`core/`), because an attacker that knows the recipe of the
challenge measures nothing.

```console
$ python -c "import os; os.rename('challenges','_x')"   # the bot survives
$ python -m bot.cli --version
challenge-captcha-bot 1.0.0
```

A test verifies it (`tests/test_bot_unit.py::TestAutonomy`): it removes the
generator package and checks the bot still imports and solves.

---

## Install

```bash
git clone git@github.com:santilpz28/Challenge-captcha.git
cd Challenge-captcha
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium      # browser mode only
```

The CLIP model (optional, ~300 MB, downloaded on first use) enables the
semantic attacker:

```bash
python -c "from challenges.clip_classifier import download; download(progress=True)"
```

---

## Usage

### From an image file

```bash
# single image: click the target
python -m bot.cli image --input captcha.png --family image --target dog

# "select all the dogs": returns EVERY match
python -m bot.cli image --input captcha.png --family image --target dog --multi

# 3x3 grid
python -m bot.cli image --input grid.png --family grid --target car

# slider (needs the piece sprite)
python -m bot.cli image --input bg.png --piece piece.png --family slider
```

### Against a JSON endpoint

```bash
python -m bot.cli api \
  --url https://target.example \
  --path /api/captcha --submit-path /api/verify \
  --family slider --submit --out report.json
```

`--submit` requires explicit confirmation: without it the bot **solves but does
not send**. The audit trail ends up in the report.

### Against a real widget (Playwright)

```bash
python -m bot.cli browser --url https://target.example --headed
```

The bot replays the human trace step by step: interpolated `mouse.move` calls,
not one teleport.

### The semantic attacker

```bash
python -m bot.cli image --input photo.png --family image --target "traffic light" --clip
```

> CLIP is **opt-in** (`--clip`) on purpose. It is the right attacker for real
> photographs ("click the dog"), but it is **worse** than classical features on
> synthetic clip-art: it labels a drawn bus as "tree". Measured: P(car)=0.04 on
> cells that are cars.

---

## Measured numbers

Run with `python3 bench.py --runs 30` (fixed seed `20261101`, 30 challenges per
family, local CPU, no GPU):

| Family | Challenge | Exact | Per unit | Cost | Attempts to pass |
|---|---|---|---|---|---|
| **slider** | drag the piece to the gap | **100%** | — | ~120 ms | **1.0** |
| **text** | "type the characters" (4–6) | **87%** | 91% per character | ~2.1 s | **1.2** |
| **grid** | "select all the images with X" (3×3) | **87%** | 96% per cell | **5 ms** | **1.2** |
| **image** | "click the image with X" | 33% | 33% | 8 ms | 3.0 |

An honest reading of the table:

- **slider**: 40/40 exact, **0 px error**. Finding the gap was never the
  problem; the position was trivial from the start.
- **text**: breaks in ~1.2 attempts. The cost is OCR inference (~2 s), not
  engineering. A retry loop passes it in under 3 seconds.
- **grid**: same result at 5 ms. Here the cost is the **arithmetic**, not the
  model — which is why the two numbers look alike: the attacker is cheap,
  multi-select is not.
- **image**: 33%. The bottleneck is **not** computer vision, it is
  *segmentation*. A useful result: it points at where the hard work is.

### The multi-select arithmetic

The result that carries the bench: the attacker keeps its per-cell accuracy
(~93%) while the **complete** accuracy collapses.

| Target cells | Exact | Per cell | Attempts |
|---|---|---|---|
| 1 | 87% | 96% | 1.2 |
| 2 | 55% | 93% | 1.8 |
| 3 | 28% | 91% | 2.7 |
| 4 | 9% | 88% | 3.9 |

This is the number a CAPTCHA provider needs: the difference between **"per
cell"** and **"per challenge"**. An attacker with 90% per cell looks mediocre;
at 9% complete challenges, it is decorative.

### The geometry that gives itself away

There's a finding faster than any computer vision, and the bot implements it as
the `geometry` strategy:

When a site cuts the piece **out of the gap** and publishes the sprite's origin
on the canvas (`sprite.origin_x/origin_y`), the gap's position travels **inside
the client payload itself**. The attacker doesn't need to look at the image: it
reads a number.

Measured against the lab: **10/10 challenges solved with 0 px error and not a
single computer-vision operation**. The bot's most expensive step — loading the
image — isn't even needed.

This isn't a flaw in the lab's generator: it's **the real pattern of a bad
implementation**, which is why it's measured and documented rather than hidden.
The lesson for a client is direct: *the client must not know the answer*. If
your endpoint publishes the gap's position, you have published the answer.

---

## The three slider strategies

| Strategy | How it works | When it wins |
|---|---|---|
| **geometry** | Reads the gap position straight out of the payload (`sprite.origin_x/y`) | When the site cuts the piece from the gap and publishes its origin. 0 px error, no computer vision |
| **template** | **Masked** normalised cross-correlation over FFT | When the sprite carries the pristine texture of the gap. 40/40 exact, 0 px, ~120 ms |
| **contrast** | The gap is a region darker than its surroundings: subtract a heavily blurred copy, threshold, find compact square-ish blobs | When the site decorates the piece or repaints the gap's interior (breaking the correlation) |

`--strategy auto` tries `geometry` → `template` → `contrast` and keeps the
first reliable one.

> **The sprite and the solver's template are two different images.** The sprite
> the user sees carries a keyline and drop shadow; the solver receives a
> **pristine** crop of the same region. Mixing them dropped accuracy from 40/40
> to 14/40.

---

## The behavior engine

A CAPTCHA that only looks at the final coordinate cannot tell a human from a
bot. The trace engine produces believable motion:

- **Fitts's law** — duration depends on distance and target size
- **easing** — acceleration and braking, not constant velocity
- **lateral arc** — the trajectory curves instead of running straight
- **tremor** — low-level noise on the points
- **overshoot and correction** — goes slightly past the target and comes back
- **monotonic timestamps** — a trace that goes backwards in time is not a
  human hesitation, it is a bug, and a kinematic scorer detects it before a
  human would

```python
from bot.behavior import human_trace
trace = human_trace(12, 120, 203, 120)   # ~45 points, ~470 ms
```

---

## Layout

```
challenge-captcha-bot/
├── bot/                  # THE ATTACKER
│   ├── cli.py            # unified CLI (image · api · browser · bench)
│   ├── solvers.py        # the 4 attackers + registry
│   ├── image_solver.py   # semantic attacker (CLIP) + multi-select
│   ├── grid_solver.py    # grid attacker
│   ├── text_solver.py    # text attacker (OCR)
│   ├── slider_solver.py  # slider attacker (NCC/FFT)
│   ├── gapdetect.py      # gap detection by local contrast
│   ├── behavior.py       # human traces (Fitts, easing, tremor, overshoot)
│   ├── client.py         # JSON HTTP client + audit trail
│   └── browser.py        # real-browser mode (Playwright)
├── core/                 # shared Challenge/Attempt contract
├── challenges/           # auxiliary generators (optional for the bot)
├── bench.py              # multi-type bench: exact rate, per unit, cost
├── tests/
│   ├── test_bot_unit.py    # 30 tests: traces, solvers, multi-select, autonomy
│   └── test_integration.py # the bot attacks a real server (12 checks)
└── docs/images/          # screenshots
```

---

## Tests

```bash
pytest tests/test_bot_unit.py -v          # 30 unit
python tests/test_integration.py           # against a live server
```

The integration test is the one that validates the product: **the bot attacks
a real server, solves, sends a human trace and is accepted**. Unit tests
validate the pieces.

To point it at your own server:

```bash
CAPTCHA_TEST_URL=https://target.example python tests/test_integration.py
```

---

## What this bot does **NOT** defeat

Honesty is part of the product. This does **not** work against:

- **CAPTCHAs with server-side attestation** (token signed with the real
  challenge state, bound to IP and session) — there is nowhere to put the
  answer.
- **Interactive challenges that depend on physics** — solving a 3D puzzle or
  tracing a curve with variable pressure.
- **Systems correlating with your reputation** — IP, cookies, account,
  history. A new bot starts at zero.
- **Aggressive rate limiting** — the bot solves fast, but it does not solve
  infinitely often.

And a reminder: **solving a CAPTCHA is not the same exploit as automating the
flow behind it**. If the goal is to abuse a service, defeating the CAPTCHA is
only the first step, and almost never the important one.

---

## Licence and use

MIT — see [LICENSE](LICENSE).

The code is for security auditing with written authorisation: your own CAPTCHA,
a client's under contract, or a lab. Using it for spam, bulk registration,
evading controls or fraud is illegal in most jurisdictions (including the GDPR
and the DMCA) and against the licence.

If someone asks you to use this tool against a system that isn't yours, that is
exactly the situation CAPTCHAs exist to prevent.
