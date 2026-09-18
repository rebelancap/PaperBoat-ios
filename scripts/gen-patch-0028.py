#!/usr/bin/env python3
"""Overlay patch 0028 - upstream's pad is SUPPRESSED on iOS, not just defaulted off.

CLASS: (a) program-baseline parity.

THE BUG THE USER SAW ON 0.0.0.8
-----------------------------
"It showed the correct touch controls for a second when first launched but
reverted to the wrong ones."

Overlay 0024 flipped `TOUCH_CONTROLS_DEFAULT` to 0 on iOS. A DEFAULT is only
consulted when the CVar is absent, and the user's phone has carried
`gTouchControls.Enabled = 1` in `paperboat.cfg.json` since 0.0.0.5, when
upstream's default was still 1 (`artifacts/device/cfg-after-0.0.0.8.json`). So
the app starts with the shell's layer (the config is not parsed yet), LUS loads
`paperboat.cfg.json` a second later, the saved 1 lands on top of the default,
and upstream's engine-drawn pad appears over ours. Exactly "correct for a
second, then reverted" - and both pads then draw and both merge into OSContPad.

A default cannot fix this. Only suppression can.

THE FIX, AND WHY IT IS IN `Enabled()`
-------------------------------------
`PaperboatGui::Enabled()` is the single truth this file already has: BOTH
`TouchControls_ApplyPad()` (:661, the input merge) and
`TouchControlsOverlay::Draw()` (:791, the render) return early on `!Enabled()`.
Gating it therefore turns upstream's pad off completely - no draw, no merge, no
finger stolen - without a second mechanism to keep in sync, and without
touching `gOpenWindows.TouchControls` (which is only that overlay window's
visibility flag; a shown window whose `Draw()` returns immediately paints
nothing, which is why a saved `TouchControls: 1` in the user's config is
harmless once this lands).

The combined truth is

    upstream's pad is on  <=>  gTouchControls.Enabled && !gSohIos.TouchLayer

and the app shell reads the SAME expression from the other side (its
`_layerOff` is now just `gSohIos.TouchLayer == 0`), so the two pads are
mutually exclusive by construction rather than by a race between two CVar
reads. `gSohIos.TouchLayer` defaults to 1 - the program's unified layer - which
is what a fresh install and every existing config get.

Switching to upstream's pad is one checkbox (Settings > Controls, relabelled
"Use Upstream Touch Pad" on iOS by overlay 0021), whose callback clears
`gSohIos.TouchLayer`. That is the ONLY way both CVars can end up pointing at
upstream's pad, and it is a deliberate act.

Desktop and Android are untouched: the whole gate is inside the iOS ifdef.

WHERE THE HUNK SITS
-------------------
One hunk, inside `Enabled()` (~:276 of the pristine file). Overlay 0024's hunks
in this file are at the very top (~:20) and appended at EOF; 0013's are at ~:112
and in SafeRect/LoadPos; 0014's are in ComputeLayout and the hideable table.
No shared context lines with any of them.

Match-count asserted against the pristine vendor state.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "src/port/ui/TouchControls.cpp"
OUT = ROOT / "overlay/patches/0028-paperboat-ios-suppress-upstream-pad.patch"

src = VENDOR / REL
orig = src.read_text()


def replace_once(t, old, new, tag):
    n = t.count(old)
    assert n == 1, f"{REL}: expected 1 match of {tag}, got {n}"
    return t.replace(old, new)


OLD = """bool Enabled() {
    RegisterCVars();
    return CVarGetInteger(CVAR_TOUCH("Enabled"), TOUCH_CONTROLS_DEFAULT) != 0;
}
"""

NEW = """bool Enabled() {
    RegisterCVars();
#if defined(PLATFORM_IOS) || defined(__IOS__)
    // PAPERBOAT_IOS (overlay 0028): while the app shell's unified touch layer
    // is the pad (gSohIos.TouchLayer, default 1), upstream's pad is OFF -
    // whatever gTouchControls.Enabled says.
    //
    // Overlay 0024 only flipped the DEFAULT, and a default loses to a saved
    // value: every config written before 0.0.0.6 carries Enabled = 1, so the
    // app came up on the right pad and swapped to the wrong one the moment LUS
    // parsed paperboat.cfg.json. This is the single read site behind both the
    // input merge (TouchControls_ApplyPad) and the draw
    // (TouchControlsOverlay::Draw), so one gate here turns the whole pad off.
    //
    // Settings > Controls > "Use Upstream Touch Pad" (overlay 0021) clears
    // gSohIos.TouchLayer, which is the one deliberate way to get here.
    if (CVarGetInteger("gSohIos.TouchLayer", 1) != 0) {
        return false;
    }
#endif
    return CVarGetInteger(CVAR_TOUCH("Enabled"), TOUCH_CONTROLS_DEFAULT) != 0;
}
"""

text = replace_once(orig, OLD, NEW, "Enabled()")

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
