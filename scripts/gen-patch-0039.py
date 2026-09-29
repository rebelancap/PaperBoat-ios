#!/usr/bin/env python3
"""Overlay patch 0039 - Full Height View defaults ON on iOS.

CLASS: (a) program-baseline parity - parity with what this port already shipped.

WHY (round 26, the upstream 1.0.2 sync, the design notes D36)
------------------------------------------------------
PaperBoat-ios 1.0.0-1.0.2 drew widescreen gameplay with NO black bands top and
bottom and with the world at its true aspect. Three overlays did that on top of
upstream 1.0.0's half-finished widescreen fit: 0029 (fill the world/battle
viewport vertically and give the projection SCREEN_WIDTH / 240), 0032 (the two
hand-painted 4:3 letterboxes the camera fix could not reach: the backdrop's
black fills and the map-change wipe) and 0035 (the widened backdrop covers the
rows it claims).

Upstream 1.0.2 replaced that whole area (#183 "Add widescreen enhancements and
fixes", then e47242c19 for the theatre curtains): the camera fit moved into
`src/port/patches/CameraPatches.cpp`, the projection aspect became
`cam_get_display_aspect()` (SCREEN_WIDTH / viewportH when widened), and the
vertical fill became an ENHANCEMENT, "Full Height View"
(`gEnhancements.Graphics.FullHeightView`, Enhancements > Graphics), default OFF.
With it on, `set_cam_viewport`'s standard 12,20,296,200 world/battle viewport is
rewritten to 12,0,296,240 - so the aspect is 320/240, exactly 0029 rev 2's
number - and because the camera FIELDS now say full height, the backdrop clamp,
the map-change wipe and the background patches (`port_cam_full_height`) all
follow on their own. That is 0029 + 0032 + 0035's user-visible result, done by
upstream's own code, so those three patches are dropped.

The one difference is the default. Upstream ships the bands on (FHV off); this
port's users have never seen them. So on iOS the default flips to ON, at the
read site AND on the menu checkbox so the menu shows the truth. Nothing else
changes: a value the player saves still wins (this is a default, not an
override), the attract demo and every cutscene/title viewport keep their own
letterbox (upstream excludes the demo and only rewrites the exact standard
rect, as 0029 did), and desktop/Android keep upstream's default.

Known cosmetic difference from 1.0.2, accepted as upstream's design: with FHV on
upstream also moves the status bar to the top edge (`port_status_bar_y`) and
drops the battle menu by 20 rows (`port_btl_menu_y`); 0029 left the HUD where
4:3 had it.

WHERE THE HUNKS SIT
-------------------
`CameraPatches.cpp` `cam_full_height_enabled()` and
`PaperboatMenuEnhancements.cpp`'s "Full Height View" widget. No other patch in
the series touches either file.

Match-count asserted against the pristine vendor state.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0039-paperboat-ios-full-height-view-default.patch"

EDITS = []

CAM_REL = "src/port/patches/CameraPatches.cpp"
CAM_OLD = """static bool cam_full_height_enabled(void) {
    return CVarGetInteger(CVAR_ENHANCEMENT("Graphics.FullHeightView"), 0) != 0 && !cam_demo_active();
}
"""
CAM_NEW = """// PAPERBOAT_IOS (overlay 0039): Full Height View defaults ON on iOS - this port
// shipped widescreen without top/bottom bands before upstream made it an
// enhancement. A saved value still wins; only the default differs.
#ifdef __IOS__
#define PB_FULL_HEIGHT_VIEW_DEFAULT 1
#else
#define PB_FULL_HEIGHT_VIEW_DEFAULT 0
#endif

static bool cam_full_height_enabled(void) {
    return CVarGetInteger(CVAR_ENHANCEMENT("Graphics.FullHeightView"), PB_FULL_HEIGHT_VIEW_DEFAULT) != 0 &&
           !cam_demo_active();
}
"""
EDITS.append((CAM_REL, [(CAM_OLD, CAM_NEW, 1)]))

MENU_REL = "src/port/ui/PaperboatMenuEnhancements.cpp"
MENU_OLD = """    AddWidget(path, "Full Height View", WIDGET_CVAR_CHECKBOX)
        .CVar(CVAR_ENHANCEMENT("Graphics.FullHeightView"))
        .Options(
            CheckboxOptions().Tooltip("Removes the letterboxing bars on the top and bottom of the screen in gameplay.")
        );
"""
MENU_NEW = """    AddWidget(path, "Full Height View", WIDGET_CVAR_CHECKBOX)
        .CVar(CVAR_ENHANCEMENT("Graphics.FullHeightView"))
        .Options(
            CheckboxOptions()
                .Tooltip("Removes the letterboxing bars on the top and bottom of the screen in gameplay.")
#ifdef __IOS__
                // PAPERBOAT_IOS (overlay 0039): must match the read site's iOS
                // default in CameraPatches.cpp, or the box shows unchecked while
                // the view is full height.
                .DefaultValue(true)
#endif
        );
"""
EDITS.append((MENU_REL, [(MENU_OLD, MENU_NEW, 1)]))

chunks = []
for rel, subs in EDITS:
    src = VENDOR / rel
    orig = src.read_text()
    text = orig
    for old, new, expect in subs:
        n = text.count(old)
        assert n == expect, f"{rel}: expected {expect} match(es) of {old!r:.70}, got {n}"
        text = text.replace(old, new, 1)
    with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
         tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
        fa.write(orig); fb.write(text); fa.flush(); fb.flush()
        r = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}",
                            fa.name, fb.name], capture_output=True)
    assert r.returncode == 1, f"{rel}: diff produced no change"
    chunks.append(r.stdout.decode())

OUT.write_text(__doc__ + "\n" + "".join(chunks))
print(f"wrote {OUT}")
