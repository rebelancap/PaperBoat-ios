#!/usr/bin/env python3
"""Overlay patch 0016 — swipe-to-scroll in the ImGui menu, and no Quit on iOS.

CLASS: (a) program-baseline parity. Both halves are candidate upstream
contributions: nothing in them is PaperBoat-ios-specific, and every line is
inside `#if defined(PLATFORM_IOS) || defined(__IOS__)`.

THE PROBLEM
-----------
Upstream's menu (`src/port/ui/Menu.cpp`) lays its pages out as ImGui child
windows with `ImGuiChildFlags_AutoResizeY` under a height constraint, i.e. they
CLIP and scroll once the content is taller than the column. On a desktop the
mouse wheel scrolls them. **A phone has no wheel**, and ImGui has no built-in
touch scrolling, so on iOS those columns are frozen: measured last round
(the measurement log M-016 notes), six `idb ui swipe` gestures over the Controls
column moved nothing while the game behind the menu kept advancing — the
gestures were delivered and ignored. The consequence is not cosmetic: overlay
0014's **Visible Buttons** checkbox list sits below the controller-bindings
block on that page, so on a phone it could not be reached at all.

THE MECHANISM, AND WHY IT IS NOT THE SOH PORT'S
-----------------------------------------------
The SoH port (Shipwright-ios overlay 0018) has a UIKit touch overlay, so its
shell queues finger deltas and `SohIos_TakeMenuScrollFor(winX, winW)` hands
them to whichever menu child the finger is horizontally inside. PaperBoat has
no UIKit touch layer to queue from (spec consequence 1 — upstream's pad is
engine-drawn), but it does not need one: SDL's UIKit backend already
synthesises mouse events from touches, which is why **taps already work** in
this menu, and `Fast3dGui::HandleWindowEvents` already scales them into the
drawable-pixel space ImGui runs in on iOS. So the finger is available as
`io.MousePos` / `io.MouseDown[0]`, and the whole patch stays in one file.

What it does, per frame, once (guarded by `ImGui::GetFrameCount()` so the
three consumers below share one update):

* Track the press: remember where it went down, and the per-frame dy.
* Below a threshold (1.5 % of display height, floor 6 px) nothing happens, so
  **a tap is still a tap** — the checkbox under the finger still toggles.
* The first frame past the threshold the press is reclassified as a drag, and
  `ImGui::ClearActiveID()` takes it away from whatever widget it landed on.
  That is what stops a swipe across a slider from dragging the slider, and a
  swipe that began on a checkbox from toggling it on release: ImGui activates
  widgets on `MouseClicked` (the down EDGE), never on `MouseDown`, so clearing
  the active id once is enough for the rest of the gesture.
* Thereafter the dy is offered to the menu's scrollable children. Each child
  asks for it right after its `BeginChild`, and only the one the finger is
  horizontally inside consumes it (the sidebar, the single-column section, and
  each of the columns are side by side and never overlap in x).

**No mouse wheel is injected.** The SoH port's device feedback was that wheel
events read as hover and even moved sliders; `SetScrollY` on the child that
owns the finger has neither effect.

THE QUIT ITEM
-------------
Upstream's menu has a red power button whose confirm popup calls
`Window::Close()`. On iOS that is not a hang — `Engine.cpp`'s loop checks
`WindowIsRunning()` at the top of every frame and calls `exit(0)` — but it is
the wrong behaviour twice over: an iOS app that vanishes is indistinguishable
from a crash to the person holding it (Apple's HIG says as much, and it is why
no shipped iOS app has a Quit button), and `exit(0)` runs neither the scene's
resign-active settings flush (overlay 0008) nor any other teardown, so it is a
way to LOSE settings that the swipe-kill path deliberately does not lose.
Hiding it on iOS is the spec's stated default and the minimal fix: three
buttons in that row become two, and the code behind the popup is untouched for
every other platform.

WHERE THE HUNKS SIT
-------------------
`apply-overlay.sh` probes "already applied" by reverse-applying at fuzz=0, so
two patches sharing context lines break the SECOND build (spec trap). This
patch's file-scope block goes at the TOP of Menu.cpp (before `namespace Ship`),
which is ~580 lines clear of overlay 0017's `extern "C"` declaration
immediately above `Menu::DrawElement`, and its edits inside `DrawElement` (the
quit row, and the three `BeginChild` sites) are all 150+ lines from 0017's two.
Menu.cpp is touched by no other patch in the series.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "src/port/ui/Menu.cpp"
OUT = ROOT / "overlay/patches/0016-paperboat-menu-touch-scroll.patch"

src = VENDOR / REL
orig = src.read_text()
text = orig


def replace_once(t, old, new, tag):
    n = t.count(old)
    assert n == 1, f"{REL}: expected 1 match of {tag}, got {n}"
    return t.replace(old, new)


# --- 1. the tracker, at file scope, above `namespace Ship` -------------------
OLD_TOP = """std::vector<SearchWidget> extraSearchWidgets = {};

namespace Ship {
"""

NEW_TOP = """std::vector<SearchWidget> extraSearchWidgets = {};

#if defined(PLATFORM_IOS) || defined(__IOS__)
// PAPERBOAT_IOS (overlay 0016): swipe-to-scroll for the menu's columns.
//
// The menu's pages are ImGui child windows that clip and scroll; a desktop
// scrolls them with the wheel and a phone has no wheel, so without this they
// are frozen and anything below the fold is unreachable. SDL's UIKit backend
// already synthesises mouse events from touches (which is why taps work), and
// Fast3dGui scales them into the drawable-pixel space ImGui runs in on iOS, so
// the finger is readable straight off ImGuiIO and no shell plumbing is needed.
namespace {

struct PbTouchScrollState {
    int frame = -1;      // last frame Update() ran, so the consumers share one
    bool down = false;   // a press is in progress
    bool drag = false;   // ... and it has been reclassified as a drag
    float startY = 0.0f; // where the press went down
    float lastY = 0.0f;
    float x = 0.0f;      // the finger's current x, for the window test
    float pending = 0.0f;
};

PbTouchScrollState sPbScroll;

void PbTouchScrollUpdate() {
    const int frame = ImGui::GetFrameCount();
    if (sPbScroll.frame == frame) {
        return;
    }
    sPbScroll.frame = frame;
    sPbScroll.pending = 0.0f;

    ImGuiIO& io = ImGui::GetIO();
    if (!io.MouseDown[ImGuiMouseButton_Left] || !ImGui::IsMousePosValid(&io.MousePos)) {
        sPbScroll.down = false;
        sPbScroll.drag = false;
        return;
    }
    const ImVec2 p = io.MousePos;
    if (!sPbScroll.down) {
        sPbScroll.down = true;
        sPbScroll.drag = false;
        sPbScroll.startY = p.y;
        sPbScroll.lastY = p.y;
        sPbScroll.x = p.x;
        return;
    }
    const float dy = p.y - sPbScroll.lastY;
    sPbScroll.lastY = p.y;
    sPbScroll.x = p.x;
    if (!sPbScroll.drag) {
        // Below the threshold this is still a tap, and the widget under the
        // finger keeps the press.
        const float threshold = ImMax(6.0f, io.DisplaySize.y * 0.015f);
        if (ImFabs(p.y - sPbScroll.startY) < threshold) {
            return;
        }
        sPbScroll.drag = true;
        // Past it, it is a scroll: take the press away from whatever widget it
        // landed on, so a swipe across a slider does not move the slider and a
        // swipe that began on a checkbox does not toggle it on release. One
        // call is enough for the whole gesture — ImGui activates widgets on the
        // mouse-down EDGE, never on the held state.
        ImGui::ClearActiveID();
    }
    sPbScroll.pending = dy;
}

// Called from INSIDE a scrollable child, right after its BeginChild. The
// finger's dy goes to the one child it is horizontally inside; the menu's
// sidebar, section and columns sit side by side and never overlap in x.
void PbTouchScrollHere() {
    PbTouchScrollUpdate();
    if (!sPbScroll.drag || sPbScroll.pending == 0.0f) {
        return;
    }
    const ImVec2 wpos = ImGui::GetWindowPos();
    const ImVec2 wsize = ImGui::GetWindowSize();
    if (sPbScroll.x < wpos.x || sPbScroll.x > wpos.x + wsize.x) {
        return;
    }
    ImGui::SetScrollY(ImGui::GetScrollY() - sPbScroll.pending);
    sPbScroll.pending = 0.0f;
}

} // namespace
#endif

namespace Ship {
"""
text = replace_once(text, OLD_TOP, NEW_TOP, "file-scope tracker")

# --- 2. the three scrollable children consume it -----------------------------
CONSUME = """#if defined(PLATFORM_IOS) || defined(__IOS__)
{indent}PbTouchScrollHere(); // overlay 0016
#endif
"""

OLD_SIDEBAR = """    ImGui::BeginChild(
        (menuEntries.at(headerIndex).label + " Section").c_str(),
        { sidebarWidth, columnHeight * 3 },
        ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize,
        ImGuiWindowFlags_NoTitleBar
    );
"""
text = replace_once(text, OLD_SIDEBAR, OLD_SIDEBAR + CONSUME.format(indent="    "), "sidebar child")

OLD_SECTION = """        ImGui::BeginChild(
            sectionMenuId.c_str(), { sectionWidth, windowHeight * 4 }, ImGuiChildFlags_AutoResizeY,
            ImGuiWindowFlags_NoTitleBar
        );
"""
text = replace_once(text, OLD_SECTION, OLD_SECTION + CONSUME.format(indent="        "), "section child")

OLD_COLUMN = """                ImGui::BeginChild(
                    sectionId.c_str(), { columnWidth, windowHeight * 4 }, ImGuiChildFlags_AutoResizeY,
                    ImGuiWindowFlags_NoTitleBar
                );
"""
text = replace_once(text, OLD_COLUMN, OLD_COLUMN + CONSUME.format(indent="                "), "column child")

# --- 3. no Quit button on iOS ------------------------------------------------
OLD_QUIT = """    ImGui::SameLine();
    UIWidgets::ButtonOptions options3 = {};
    options3.color = UIWidgets::Colors::Red;
    options3.size = UIWidgets::Sizes::Inline;
    options3.tooltip = "Quit Paperboat";
    if (UIWidgets::Button(ICON_FA_POWER_OFF, options3)) {
        PaperboatGui::mModalWindow->RegisterPopup(
            "Quit Paperboat", "Are you sure you want to quit Paperboat?", "Quit", "Cancel",
            []() {
                std::shared_ptr<Menu> menu =
                    static_pointer_cast<Menu>(Ship::Context::GetRawInstance()->GetWindow()->GetGui()->GetMenu());
                if (!menu->IsMenuPopped()) {
                    menu->ToggleVisibility();
                }
                Ship::Context::GetRawInstance()->GetWindow()->Close();
            },
            nullptr
        );
    }
    ImGui::PopStyleVar();
"""

NEW_QUIT = """#if !defined(PLATFORM_IOS) && !defined(__IOS__)
    // PAPERBOAT_IOS (overlay 0016): no Quit item on iOS. Close() is not a hang
    // here — the engine loop sees !WindowIsRunning() and calls exit(0) — but an
    // app that vanishes reads as a crash to the person holding the phone, and
    // exit(0) runs neither the scene's resign-active settings flush nor any
    // other teardown, so it is a way to lose settings that swipe-killing the
    // app does not lose. Every other platform is untouched.
    ImGui::SameLine();
    UIWidgets::ButtonOptions options3 = {};
    options3.color = UIWidgets::Colors::Red;
    options3.size = UIWidgets::Sizes::Inline;
    options3.tooltip = "Quit Paperboat";
    if (UIWidgets::Button(ICON_FA_POWER_OFF, options3)) {
        PaperboatGui::mModalWindow->RegisterPopup(
            "Quit Paperboat", "Are you sure you want to quit Paperboat?", "Quit", "Cancel",
            []() {
                std::shared_ptr<Menu> menu =
                    static_pointer_cast<Menu>(Ship::Context::GetRawInstance()->GetWindow()->GetGui()->GetMenu());
                if (!menu->IsMenuPopped()) {
                    menu->ToggleVisibility();
                }
                Ship::Context::GetRawInstance()->GetWindow()->Close();
            },
            nullptr
        );
    }
#endif
    ImGui::PopStyleVar();
"""
text = replace_once(text, OLD_QUIT, NEW_QUIT, "quit button")

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
