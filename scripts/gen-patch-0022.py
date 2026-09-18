#!/usr/bin/env python3
"""Overlay patch 0022 - the iOS menu scale: the slider, AND the pixel-space
correction that makes the family's default land at the siblings' size.

CLASS: (a) program-baseline parity. The engine half of the "Menu Scale" widget
overlay 0021 adds (checklist row 7); cross-port adoption of Lighthouse's 0045,
which is itself the SoH port's 0015 - plus one factor no sibling needs.

WHAT IT DOES
------------
`GameEngine::ScaleImGui()` maps the `gSettings.ImGuiScale` PRESET INDEX through
`imguiScaleOptionToValue[]` and applies the ratio against the previous scale.
On iOS the effective scale becomes

    imguiScaleOptionToValue[preset] * gSohIos.MenuScale * PBIos_DisplayScale()

i.e. the family's two factors (preset default 1 -> 1.0, MenuScale default 0.85,
byte-for-byte the siblings' defaults) times this fork's drawable-pixels-per-
point ratio.

WHY THAT THIRD FACTOR, AND WHY ONLY HERE
----------------------------------------
Every sibling runs ImGui in POINT space - DisplaySize 912x420 with
DisplayFramebufferScale 3x, which is exactly what Lighthouse's overlay 0010
exists to arrange - so 0.85 is a readable physical size there. THIS fork runs
ImGui in DRAWABLE PIXELS with DisplayFramebufferScale (1,1)
(`Fast3dGui::ImGuiWMNewFrame`'s `__IOS__` branch; the design notes D16, and the reason
overlays 0013/0017 spend safe-area insets in pixels). The same 0.85 therefore
renders every glyph, button and modal at ONE THIRD of its size on the siblings.
That is the "the libultraship 'no o2r files' window is way too small" reported
against 0.0.0.5 - the defaults were already the family's; the space they were
being spent in was not.

Multiplying by the ratio restores the siblings' apparent size while keeping the
family's defaults literally the family's, so the slider still reads 85 % and
means what it means everywhere else.

IT HAS TO APPLY AT INIT, NOT WHEN THE MENU OPENS. `ScaleImGui()` is called at
the end of `GameEngine::GameEngine()` (right after the fonts are created), and
`RunExtract` - which puts up the "No O2R Files" modal - runs later, from
`GameEngine_Init`. So the first-run modal is already scaled the first time it
is drawn, on a container with no config at all. That ordering is upstream's and
this patch does not change it; it is the reason no new call site is needed.

THE TRAP, AND IT HAS BITTEN TWICE
---------------------------------
The function early-outs on `imGuiScaleIndex == previousImGuiScaleIndex`. A
slider move does not change the preset index, so with the early-out left as it
is the slider silently does nothing - which is exactly the bug the SoH port
shipped and had to fix on device, and which Lighthouse then re-discovered. So
on iOS the comparison is against the COMBINED float (`preset * slider`), which
moves whenever either half does.

`previousImGuiScale` is already a float carrying the last applied combined
value, so the ratio arithmetic below the guard is unchanged.

Inert off iOS: every other platform keeps the index comparison byte-for-byte.

WHERE THE HUNK SITS
-------------------
One hunk, inside `GameEngine::ScaleImGui` (~:357 of the pristine file). The
other patches in this file are overlay 0004 (~:190) and overlay 0023 (~:994),
and overlay 0012's three hunks are at ~:1137+. No shared context.

Match-count asserted against the pristine vendor state.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "src/port/Engine.cpp"
OUT = ROOT / "overlay/patches/0022-paperboat-ios-menu-scale.patch"

src = VENDOR / REL
orig = src.read_text()
text = orig


def replace_once(t, old, new, tag):
    n = t.count(old)
    assert n == 1, f"{REL}: expected 1 match of {tag}, got {n}"
    return t.replace(old, new)


OLD = """
void GameEngine::ScaleImGui() {
    int32_t imGuiScaleIndex = CVarGetInteger("gSettings.ImGuiScale", defaultImGuiScale);
    if (imGuiScaleIndex == previousImGuiScaleIndex) {
        return;
    }

    float scale = imguiScaleOptionToValue[imGuiScaleIndex];
    float newScale = scale / previousImGuiScale;
"""

NEW = """
#ifdef __IOS__
// PAPERBOAT_IOS (overlay 0022): drawable pixels per point, from the app shell
// (app/ios/PaperBoatIosShell.m). This fork runs ImGui in DRAWABLE PIXELS with
// DisplayFramebufferScale (1,1) on iOS, unlike every sibling port, so the
// family's menu scale has to be multiplied by this ratio to land at the same
// physical size. Without it the whole menu - and the first-run "No O2R Files"
// modal - renders at one third size. See the patch header.
extern "C" float PBIos_DisplayScale(void);
#endif

void GameEngine::ScaleImGui() {
    int32_t imGuiScaleIndex = CVarGetInteger("gSettings.ImGuiScale", defaultImGuiScale);
#ifdef __IOS__
    // Fold the iOS menu-scale slider (overlay 0021, Settings > iOS > Menu) into
    // the preset, and correct for pixel space. Compare the COMBINED scale, not
    // the preset index: a slider move leaves the index alone, so keeping the
    // index early-out would make the slider silently do nothing - the exact bug
    // the SoH port shipped and had to fix on device.
    float scale =
        imguiScaleOptionToValue[imGuiScaleIndex] * CVarGetFloat("gSohIos.MenuScale", 0.85f) * PBIos_DisplayScale();
    if (scale == previousImGuiScale) {
        return;
    }
    float newScale = scale / previousImGuiScale;
#else
    if (imGuiScaleIndex == previousImGuiScaleIndex) {
        return;
    }

    float scale = imguiScaleOptionToValue[imGuiScaleIndex];
    float newScale = scale / previousImGuiScale;
#endif
"""
text = replace_once(text, OLD, NEW, "ScaleImGui prologue")

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
