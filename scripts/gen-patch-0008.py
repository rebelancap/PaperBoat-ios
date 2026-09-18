#!/usr/bin/env python3
"""Overlay patch 0008 — do not render while the app is backgrounded (iOS).

CLASS: (a) program-baseline parity.

The rule this enforces is old and expensive: **Metal's `nextDrawable` must not
be called while the process is backgrounded.** It stalls (seconds per frame),
background GPU work risks a watchdog kill, and the drawable pool can come back
degraded — the SoH port's device symptom was "the game is unplayably slow after
minimise + reopen" (Shipwright-ios overlay 0016).

PaperBoat's LUS already has the right SHAPE of gate. `Fast3dWindow.cpp` skips
the entire render + present when the window is not visible:

    if (!mWindowManagerApi->IsWindowVisible()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(8));
        return false;
    }

and that is strictly better than the SoH port's park-inside-the-backend,
because the game loop keeps running (and therefore keeps pumping SDL events,
which on iOS is what services the main run loop and delivers the very
notifications that un-background us — a sleeping park there deadlocks into a
black screen, which is why SoH port 0016 needs its ParkTick).

What is missing is the iOS half of `IsWindowVisible()`. It asks SDL for
`SDL_WINDOW_MINIMIZED | SDL_WINDOW_HIDDEN`, and SDL 2.32.10's UIKit backend sets
neither for a scene-based app: it has no UIScene code at all (that is why this
port needs overlay 0001 in the first place), so nothing in SDL ever learns that
the app went to the background. The macOS branch below it queries Cocoa directly
for occlusion for the same reason — this is the iOS sibling of that call.

The flag comes from the port's shell (`app/ios/PaperBoatIosShell.m`), which sets
it in `sceneDidEnterBackground` / `UISceneDidEnterBackgroundNotification` and
clears it on every foreground path (three independent clears — a flag stuck at 1
is a permanently black screen).

Why the vendor edit is one `#ifdef __IOS__` block and not more: the flag is read
where visibility is ALREADY decided, so no other code changes and there is no
second definition of "visible" to keep in sync.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "external/libultraship/src/fast/backends/gfx_sdl2.cpp"
OUT = ROOT / "overlay/patches/0008-lus-ios-no-render-while-backgrounded.patch"

src = VENDOR / REL
orig = src.read_text()

OLD = """bool GfxWindowBackendSDL2::IsWindowVisible() {
    const uint32_t flags = SDL_GetWindowFlags(mWnd);
    if (flags & (SDL_WINDOW_MINIMIZED | SDL_WINDOW_HIDDEN)) {
        return false;
    }
"""

NEW = """#ifdef __IOS__
// PAPERBOAT_IOS (overlay 0008): the app shell's backgrounded flag. Linkage
// specifications must sit at file scope, not inside the function.
extern "C" int PBIos_IsBackgrounded(void);
#endif

bool GfxWindowBackendSDL2::IsWindowVisible() {
    const uint32_t flags = SDL_GetWindowFlags(mWnd);
    if (flags & (SDL_WINDOW_MINIMIZED | SDL_WINDOW_HIDDEN)) {
        return false;
    }
#ifdef __IOS__
    // SDL2's UIKit backend has no UIScene code, so it never sets MINIMIZED or
    // HIDDEN for a scene-based app and the flags above cannot see a
    // backgrounded app at all. Ask the shell, which observes the scene
    // lifecycle directly. Metal's nextDrawable in the background stalls for
    // seconds per frame and risks a watchdog kill; the caller
    // (Fast3dWindow::DrawAndRunGraphicsCommands) skips the whole frame and
    // naps 8 ms, which keeps the loop pumping SDL events — and pumping events
    // is what services the iOS run loop that delivers the foreground callback.
    if (PBIos_IsBackgrounded()) {
        return false;
    }
#endif
"""

n = orig.count(OLD)
assert n == 1, f"{REL}: expected 1 match of IsWindowVisible's head, got {n}"
text = orig.replace(OLD, NEW)

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
