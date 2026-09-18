#!/usr/bin/env python3
"""Overlay patch 0031 - the menu header row: no scrollbar drawn over the tabs.

CLASS: (a) program-baseline parity. The underlying arithmetic bug is upstream's
and would be class (b), but only a phone's menu scale reaches the state, and a
fix for every platform would change desktop geometry - so this is `__IOS__`-gated
and nothing else moves.

THE REPORT
----------
The user, on 0.0.0.9: a horizontal scrollbar is drawn straight ACROSS
"Settings Enhancements Shaders Dev Tools Search...", cutting the labels in
half. Reproduced on the simulator at the default menu scale
(`gSohIos.MenuScale` 0.85 x display scale 3 = 2.55, the design notes D24):
`artifacts/sim/m028-c-menu-before.png`.

WHY IT HAPPENS
--------------
`Menu::Draw` measures the header row and decides for itself whether it needs a
horizontal scrollbar (`Menu.cpp` ~:745 pristine):

    float headerHeight = headerSizes.at(0).y + style.FramePadding.y * 2;
    if (headerWidth > menuSize.x - buttonSize.x * 3 - style.ItemSpacing.x * 3) {
        headerHeight += style.ScrollbarSize;
        scrollbar = true;
    }
    ImGui::SetNextWindowSizeConstraints({ 0, headerHeight }, { headerWidth, headerHeight });
    ImVec2 headerSelSize = { menuSize.x - ..., headerHeight };
    if (scrollbar) headerSelSize.y += style.ScrollbarSize;

The extra `style.ScrollbarSize` the child asks for is immediately clamped away
again by the size constraint one line above, whose MAX y is also `headerHeight`
- so the room the scrollbar needs is never actually granted and ImGui draws the
bar over the last rows of text. On desktop nobody sees it, because the header
never overflows there.

On a phone it DOES overflow, at the default menu scale (2.55 on this fork -
this LUS runs ImGui in drawable pixels, D24), because `headerWidth` is measured
from the scaled font while the room left for it is the menu block minus three
icon buttons.

THE FIX, iOS ONLY
-----------------
The header child takes `NoScrollbar | NoScrollWithMouse` and `scrollbar` stays
false, so its height is the text row and nothing else and no bar is ever drawn
over the labels. The row is still scrollable - by drag (overlay 0016's touch
scroll) and programmatically - only the bar is gone; if the content is wider
than the row, the labels clip at the edge the way a phone tab bar clips,
instead of being struck through. Measured on the simulator at the default
scale, before/after: `artifacts/sim/m028-c-menu-before.png` (a bar across
"Settings Enhancements Shaders Dev Tools Search...", labels cut in half) vs
`artifacts/sim/m028-e-menu-after.png` (no bar, all five labels whole).

Not done: rewriting upstream's constraint arithmetic for every platform. The
clamp is a real upstream bug, but no desktop layout reaches it, and a patch
that changes desktop geometry is a patch this port has to defend on every
upstream bump. Also considered and dropped: lifting the menu's 16:9 width cap
(`fminf(windowWidth * 0.9f, windowHeight * 1.77f)`) on iOS. Measured, the cap is
NOT the binding term at this WorkRect (overlay 0017 has already inset it to
~2328x1200, where 0.9*w = 2095 < 1.77*h = 2124), so it would have changed
nothing and is not in the patch.

WHERE THE HUNKS SIT
-------------------
Two hunks, pristine ~:734 and ~:747, both inside the header child's setup. The
other two patches in this file are clear of both: overlay 0017 at pristine
603/628/651 and overlay 0016 at pristine 815/834/905/953/994.

Match-count asserted against the pristine vendor state.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "src/port/ui/Menu.cpp"
OUT = ROOT / "overlay/patches/0031-paperboat-ios-menu-header-fit.patch"

src = VENDOR / REL
orig = src.read_text()
text = orig


def replace_once(t, old, new, tag):
    n = t.count(old)
    assert n == 1, f"{REL}: expected 1 match of {tag}, got {n}"
    return t.replace(old, new)


HEADER_OLD = """    bool scrollbar = false;
    if (headerWidth > menuSize.x - buttonSize.x * 3 - style.ItemSpacing.x * 3) {
        headerHeight += style.ScrollbarSize;
        scrollbar = true;
    }
"""

HEADER_NEW = """    bool scrollbar = false;
#if defined(PLATFORM_IOS) || defined(__IOS__)
    // PAPERBOAT_IOS (overlay 0031): the header never grows a scrollbar here.
    //
    // Upstream's arithmetic cannot work: the `headerSelSize.y +=
    // style.ScrollbarSize` below is clamped straight back off by the
    // SetNextWindowSizeConstraints whose MAX y is also headerHeight, so the room
    // the bar needs is never granted and ImGui draws it over the last rows of
    // the tab labels. Without the bar the row is the text row; if the content
    // is ever wider than the row, the labels clip at the edge the way a phone
    // tab bar clips. The row is still scrollable - by drag
    // (overlay 0016) and programmatically - only the bar is gone.
    (void)headerWidth;
#else
    if (headerWidth > menuSize.x - buttonSize.x * 3 - style.ItemSpacing.x * 3) {
        headerHeight += style.ScrollbarSize;
        scrollbar = true;
    }
#endif
"""
text = replace_once(text, HEADER_OLD, HEADER_NEW, "header scrollbar decision")

FLAGS_OLD = """        ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_HorizontalScrollbar
    );
"""

FLAGS_NEW = """#if defined(PLATFORM_IOS) || defined(__IOS__)
        // PAPERBOAT_IOS (overlay 0031): see above - no bar on the header row.
        ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse
#else
        ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_HorizontalScrollbar
#endif
    );
"""
text = replace_once(text, FLAGS_OLD, FLAGS_NEW, "header child flags")

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
