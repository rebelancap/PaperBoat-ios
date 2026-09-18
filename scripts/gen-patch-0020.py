#!/usr/bin/env python3
"""Overlay patch 0020 - pin Internal Resolution at 100% and grey it out on iOS.

CLASS: (a) program-baseline parity. Cross-port adoption of Lighthouse's overlay
0053 (SpaghettiKart 0047, Shipwright 0024, 2ship 0040 are the same idea).

WHY THE SLIDER CANNOT MOVE A PIXEL HERE
---------------------------------------
`CVAR_INTERNAL_RESOLUTION` lands in `Interpreter::mCurDimensions.internal_mul`,
read in exactly one place: `Fast3dGui::CalculateGameViewport`, where it scales
`mCurDimensions` from the "Main Game" ImGui window's content size. Overlay 0019
then immediately recomputes the same fields in `Interpreter::StartFrame` as

    f = (nativeDrawablePx / mCurDimensions.width) * ssaa
    mCurDimensions *= f

Write it out and `mCurDimensions.width` cancels: the result is
`nativePx * ssaa` for width and `height * nativePx / width * ssaa` for height,
both independent of `internal_mul` - 0019 divides it straight back out. The
factor is uniform, so the aspect ratio is unaffected too.

Left alone the slider is worse than useless: it looks like THE resolution
control, and a user who drags it and sees nothing change concludes the port
ignores its own graphics settings. A value persisted from a desktop config
would also ride along silently in the CVar with nothing honouring it.

So: pin the CVar to 1.0, disable the widget, and point at the control that does
work (Supersampling, Settings > iOS - overlay 0021).

MECHANICS (the part that is easy to get wrong)
----------------------------------------------
`Menu::MenuDrawItem` calls `widget.ResetDisables()` and THEN `widget.preFunc()`
(src/port/ui/Menu.cpp:401-402), so a build-time `Options(...).Disabled(true)`
is clobbered every draw. Assigning `options->disabled` / `disabledTooltip`
INSIDE PreFunc sticks. Returning early also skips the `!activeDisables.empty()`
branch below, which would otherwise overwrite `disabledTooltip` with the
generic "This setting is disabled because:" text built from `disabledTempTooltip`.
`disabledTooltip` is a `const char*` held past the call, so it must be a string
literal (static lifetime), never a temporary.

ASCII ONLY: the menu's font atlas has no em-dash or bullet glyph and renders
them as '?'.

TEXT DELTA vs the checklist: the Vision Pro sentence is dropped (no visionOS
target on this port), and with it the now-dangling "iPhone:" label - the
remaining sentence reads as the instruction it is. The design notes D23.

`__IOS__`-guarded, so desktop and the macOS oracle keep the working slider.

WHERE THE HUNK SITS
-------------------
One hunk, inside the Internal Resolution widget's PreFunc (Settings > Graphics,
~:300 of the pristine file). Overlay 0014's hunk in this file is ~50 lines
above (Settings > Controls) and overlay 0021's are at the top of the file and
in Settings > General, ~150 lines above. No shared context.

Match-count asserted against the pristine vendor state.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "src/port/ui/PaperboatMenuSettings.cpp"
OUT = ROOT / "overlay/patches/0020-paperboat-internal-res-lock.patch"

src = VENDOR / REL
orig = src.read_text()
text = orig


def replace_once(t, old, new, tag):
    n = t.count(old)
    assert n == 1, f"{REL}: expected 1 match of {tag}, got {n}"
    return t.replace(old, new)


OLD = """        .PreFunc([](WidgetInfo& info) {
            if (mPaperboatMenu->disabledMap.at(DISABLE_FOR_ADVANCED_RESOLUTION_ON).active
                && mPaperboatMenu->disabledMap.at(DISABLE_FOR_VERTICAL_RES_TOGGLE_ON).active)
"""

NEW = """        .PreFunc([](WidgetInfo& info) {
#ifdef __IOS__
            // PAPERBOAT_IOS (overlay 0020): inert on this platform. The
            // multiplier only drives the Fast3d ImGui game-window scale, which
            // overlay 0019 recomputes from the native drawable size on the very
            // next line of StartFrame - internal_mul cancels out of the result.
            // Pin it at 100% and grey it out rather than leave a slider that
            // visibly does nothing. Set here and not at build time because
            // MenuDrawItem calls ResetDisables() immediately before preFunc;
            // return early so the activeDisables branch cannot overwrite the
            // tooltip. ASCII only - the menu atlas has no em-dash.
            if (CVarGetFloat(CVAR_INTERNAL_RESOLUTION, 1.0f) != 1.0f) {
                CVarSetFloat(CVAR_INTERNAL_RESOLUTION, 1.0f);
                Ship::Context::GetRawInstance()->GetWindow()->SetResolutionMultiplier(1.0f);
            }
            info.options->disabled = true;
            info.options->disabledTooltip =
                "Automatic on this platform - the game already renders at native "
                "resolution, so this multiplier does nothing. Use Supersampling "
                "in Settings > iOS.";
            return;
#endif
            if (mPaperboatMenu->disabledMap.at(DISABLE_FOR_ADVANCED_RESOLUTION_ON).active
                && mPaperboatMenu->disabledMap.at(DISABLE_FOR_VERTICAL_RES_TOGGLE_ON).active)
"""
text = replace_once(text, OLD, NEW, "Internal Resolution PreFunc")

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
