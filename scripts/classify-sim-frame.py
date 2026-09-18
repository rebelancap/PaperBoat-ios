#!/usr/bin/env python3
"""Classify one simulator screenshot: is this the title screen, a game frame,
an in-engine modal prompt, or the springboard?

    classify-sim-frame.py <frame.png> <title-ref.png> [springboard.png]
    -> "<kind> <title-score> <unique-colours>"   e.g. "title 0.982 35671"

Why these two measures and not "is the frame black" (round 2's test, which
passed on a picture of a dialog):

  * UNIQUE COLOURS separates an in-engine modal from a rendered game frame,
    and it is not a close call. Upstream's first-run prompts are a small ImGui
    window on a flat clear colour: ~380 distinct colours at 456x210. Any frame
    the game actually renders is 5k-41k. Measured 2026-09-16 over the whole
    first-run flow and a five-minute attract demo.
  * TITLE SCORE is the normalised cross-correlation of the 64x64 greyscale
    frame against a known title-screen capture. The title screen scores 0.98;
    the intro, the prologue and every attract-demo scene score below 0.67.
    Greyscale at 64x64 also makes it tolerant of the touch overlay being on or
    off, which is a setting and not a boot property.

Kinds: title | game | modal | springboard | blank
"""
import sys
import numpy as np
from PIL import Image

MODAL_MAX_COLOURS = 2000   # modal ~380, quietest real frame ~4757
TITLE_MATCH = 0.90         # title 0.98, next-best scene 0.67


def gray_feat(path):
    a = np.asarray(Image.open(path).convert("L").resize((64, 64), Image.LANCZOS),
                   dtype=np.float64).ravel()
    return (a - a.mean()) / (a.std() + 1e-6)


def same(a_path, b_path, tol=0.001):
    """True (exit 0) when two frames are the same picture — used to confirm that
    a tap actually did something before the next one is aimed.

    The tolerance is DELIBERATELY tiny (0.1% of pixels). Consecutive first-run
    prompts differ only inside a dialog that covers ~0.7% of the frame, so a
    2%-of-pixels threshold called two different prompts "the same" and the
    script declared the coordinates stale while they were in fact working."""
    a = np.asarray(Image.open(a_path).convert("RGB").resize((456, 210), Image.LANCZOS),
                   dtype=np.int16)
    b = np.asarray(Image.open(b_path).convert("RGB").resize((456, 210), Image.LANCZOS),
                   dtype=np.int16)
    if a.shape != b.shape:
        return False
    return float((np.abs(a - b).max(axis=2) > 16).mean()) < tol


def main():
    if sys.argv[1] == "--same":
        ok = same(sys.argv[2], sys.argv[3])
        print("same" if ok else "diff")
        return 0 if ok else 1

    frame, ref = sys.argv[1], sys.argv[2]
    base = sys.argv[3] if len(sys.argv) > 3 else None

    rgb = np.asarray(Image.open(frame).convert("RGB").resize((456, 210), Image.LANCZOS),
                     dtype=np.uint8)
    uniq = len(np.unique(rgb.reshape(-1, 3), axis=0))
    score = float((gray_feat(ref) * gray_feat(frame)).mean())

    if base is not None:
        b = np.asarray(Image.open(base).convert("RGB").resize((456, 210), Image.LANCZOS),
                       dtype=np.int16)
        differs = float((np.abs(rgb.astype(np.int16) - b).max(axis=2) > 16).mean())
    else:
        differs = 1.0

    if score >= TITLE_MATCH:
        kind = "title"
    elif uniq > MODAL_MAX_COLOURS:
        kind = "game"
    elif differs < 0.10:
        kind = "springboard"
    elif rgb.max() <= 12:
        kind = "blank"
    else:
        kind = "modal"

    print(f"{kind} {score:.3f} {uniq}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
