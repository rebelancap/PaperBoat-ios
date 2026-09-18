#!/usr/bin/env python3
"""Overlay patch 0017 — the ImGui menu respects the iOS safe area.

CLASS: (a) program-baseline parity. Upstreamable as it stands (the only
PaperBoat-ios-specific thing in it is the name of the shell symbol, exactly as
in overlay 0013).

THE PROBLEM
-----------
`Menu::DrawElement` sizes its window from the main viewport's WorkSize and
centres it on the viewport's centre, i.e. against the FULL display. On an
iPhone in landscape the sensor housing eats ~68 pt off each side and the home
indicator ~20 pt off the bottom, so the menu's leading column — the sidebar —
runs under the housing. Recorded last round as a known gap
(`docs/touch-layout.md` §6, the measurement log M-016 notes); this is the fix.

Overlay 0013 did this for the engine-drawn touch pad. Same source of truth,
same units: the shell's

    void PBIos_GetSafeAreaInsets(float* top, float* left, float* bottom, float* right);

in **drawable pixels**, read from a probe UIWindow on the scene rather than
from SDL's window (which reports the portrait inset set on a landscape window —
the design notes D16). Pixels because this LUS fork runs ImGui in drawable-pixel space
on iOS (`io.DisplaySize` from `SDL_GetRendererOutputSize`,
`DisplayFramebufferScale = (1,1)`), which is the same space the viewport's
WorkSize is in.

WHAT IT DOES
------------
Two numbers, one place. The menu's size is reduced by the insets and its centre
moves to the centre of the safe rect, so **everything inside it** — the header
row, the sidebar, the section columns, the popup modals that centre on it —
comes along for free; there is no per-widget inset arithmetic anywhere. The
same defensive guard as 0013: a nonsense inset (a window caught mid-rotation)
that would collapse the menu to less than half the screen is ignored.

Zero insets, and every non-iOS platform, are byte-identical to upstream: the
new `pbMenuCenter` is initialised to `GetMainViewport()->GetCenter()`, which is
exactly what the call it replaces passed.

NOT DONE HERE
-------------
* The popout ("Menu.Popout") path keeps its saved size and position untouched.
  It is a desktop feature (a second OS window); on iOS it is not reachable and
  clamping a persisted popout rect would rewrite a desktop user's saved value
  the next time they built for both.
* The menu background no longer covers the safe-area strips, so the game shows
  through them. That is what respecting the safe area looks like; the
  alternative (a full-bleed background with inset content) needs a second
  window and buys nothing on a device.

WHERE THE HUNKS SIT
-------------------
`apply-overlay.sh` probes "already applied" by reverse-applying at fuzz=0, so
two patches sharing context lines break the SECOND build (spec trap). All
three hunks here are inside or immediately above `Menu::DrawElement`
(~lines 605-655 of upstream's file); overlay 0016's are at the very top of the
file and at lines 820+/900+. Nothing else in the series touches Menu.cpp.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "src/port/ui/Menu.cpp"
OUT = ROOT / "overlay/patches/0017-paperboat-menu-safe-area.patch"

src = VENDOR / REL
orig = src.read_text()
text = orig


def replace_once(t, old, new, tag):
    n = t.count(old)
    assert n == 1, f"{REL}: expected 1 match of {tag}, got {n}"
    return t.replace(old, new)


# --- 1. the shell symbol, at namespace scope (a linkage spec may not sit
#        inside a function) ---------------------------------------------------
OLD_EXTERN = """static bool freshOpen = true;
void Menu::DrawElement() {
"""

NEW_EXTERN = """#if defined(PLATFORM_IOS) || defined(__IOS__)
// PAPERBOAT_IOS (overlay 0017): the app shell's safe-area insets, in DRAWABLE
// PIXELS — the space ImGui runs in on iOS in this fork, and therefore the space
// the main viewport's WorkSize/WorkPos are in. Same symbol overlay 0013 hands
// the engine-drawn touch pad; the design notes D16 for why it comes from a probe
// window and not from SDL's.
extern "C" void PBIos_GetSafeAreaInsets(float* top, float* left, float* bottom, float* right);
#endif

static bool freshOpen = true;
void Menu::DrawElement() {
"""
text = replace_once(text, OLD_EXTERN, NEW_EXTERN, "extern declaration")

# --- 2. shrink the menu onto the safe rect -----------------------------------
OLD_SIZE = """    windowHeight = ImGui::GetMainViewport()->WorkSize.y;
    windowWidth = ImGui::GetMainViewport()->WorkSize.x;
"""

NEW_SIZE = """    windowHeight = ImGui::GetMainViewport()->WorkSize.y;
    windowWidth = ImGui::GetMainViewport()->WorkSize.x;
    ImVec2 pbMenuCenter = ImGui::GetMainViewport()->GetCenter();
#if defined(PLATFORM_IOS) || defined(__IOS__)
    {
        // Keep the whole menu out from under the sensor housing and the home
        // indicator. Doing it once, on the window, covers the header row, the
        // sidebar, the section columns and the modals that centre on it.
        float pbTop = 0.0f, pbLeft = 0.0f, pbBottom = 0.0f, pbRight = 0.0f;
        PBIos_GetSafeAreaInsets(&pbTop, &pbLeft, &pbBottom, &pbRight);
        const ImVec2 pbWorkPos = ImGui::GetMainViewport()->WorkPos;
        // Defensive, as in overlay 0013: a nonsense inset (a window caught
        // mid-rotation) must never collapse the menu to a sliver.
        if (pbTop >= 0.0f && pbLeft >= 0.0f && pbBottom >= 0.0f && pbRight >= 0.0f
            && (windowWidth - pbLeft - pbRight) > windowWidth * 0.5f
            && (windowHeight - pbTop - pbBottom) > windowHeight * 0.5f)
        {
            windowWidth -= pbLeft + pbRight;
            windowHeight -= pbTop + pbBottom;
            pbMenuCenter =
                ImVec2(pbWorkPos.x + pbLeft + windowWidth * 0.5f, pbWorkPos.y + pbTop + windowHeight * 0.5f);
        }
    }
#endif
"""
text = replace_once(text, OLD_SIZE, NEW_SIZE, "viewport size block")

# --- 3. centre on the safe rect ----------------------------------------------
OLD_POS = """        ImGui::SetNextWindowPos(ImGui::GetMainViewport()->GetCenter(), windowCond, { 0.5f, 0.5f });
"""
NEW_POS = """        ImGui::SetNextWindowPos(pbMenuCenter, windowCond, { 0.5f, 0.5f });
"""
text = replace_once(text, OLD_POS, NEW_POS, "SetNextWindowPos")

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
