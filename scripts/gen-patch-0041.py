#!/usr/bin/env python3
"""Overlay patch 0041 - spike attribution probe + PB_PACE line (iOS only).

CLASS: (a) program-baseline parity (the perf rollout of 2026-10-09; Shipwright
0065's SOH_PACE + 0066/0068's spike probe and menu split, D-084/D-085/D-087).
The design notes D40.

WHAT. A new file, src/port/ios/PBIosSpikeProbe.cpp (source of truth:
overlay/files/PBIosSpikeProbe.cpp; picked up by upstream's GLOB_RECURSE of
src/*.cpp, and #ifdef __IOS__ throughout), plus the seams that feed it:

  Fast3dWindow.cpp   frame begin (after the visibility check), marks after
                     Gui::StartDraw / Interpreter::Run / Gui::EndDraw, frame end
                     after Interpreter::EndFrame; HandleEvents -> `ev`
  gfx_metal.cpp      SetupScreenFramebuffer inside NewFrame -> `wait`;
                     ReadFramebufferToCPU -> `fbrd`
  gfx_sdl2.cpp       SyncFramerateWithTime inside SwapBuffersBegin -> `sleep`
  Gui.cpp            the menu-phase split (StartFrame: nf/wm/ig; DrawMenu:
                     dock/bar/menu/win, every GuiWindow timed by name);
                     CheckSaveCvars -> `cvsave`
  SDLAddRemoveDeviceEventHandler.cpp   its per-frame SDL_PumpEvents -> `pump`
  ResourceManager.cpp  a cache-miss wait in LoadResource -> `res`
  Game.cpp           OS_SiService -> `si`; the no-task SDL_Delay(1) -> `idle`

Output lines PB_PACE / PB_SPKWIN / PB_SPIKE / PB_MENUWIN every 10 s (formats
in the file header and the measurement log M-039). `gSohIos.SpikeProbe` (default 1)
switches it off. Reads overlay 0012 rev 9's PBIos_PaceSnapshot and
gPBIosSubframesPerTick, and the shell's PBIos_ThermalState/PBIos_PowerState.

NO GetPixelDepth: PM64 never calls it (grep of src/ and the LUS fork), so
Shipwright's change 3 (async depth + PSO cache, 0067) does not apply; the only
GPU->CPU read is the flame effect's gDPReadFBToI8, timed here as `fbrd` (D41).

Generate against the vendor tree with 0001..0040 applied and 0041 not applied.
Every hunk keeps clear of earlier patches' hunks and their 3-line contexts
(apply-overlay.sh's already-applied probe proves it on every build).
"""
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0041-paperboat-ios-spike-probe.patch"
PROBE_SRC = ROOT / "overlay/files/PBIosSpikeProbe.cpp"
LUS = "external/libultraship"

DECL = """#ifdef __IOS__
// PAPERBOAT_IOS (overlay 0041, D40): spike probe seams; the probe lives in
// src/port/ios/PBIosSpikeProbe.cpp. PBIos_SpkNow returns 0 while the probe is
// off or off the main thread, and PBIos_SpkAdd ignores a 0 start.
extern "C" unsigned long long PBIos_SpkNow(void);
extern "C" void PBIos_SpkAdd(int cat, unsigned long long t0);
#endif
"""

EDITS = []

# --- Fast3dWindow.cpp -------------------------------------------------------
F3D = f"{LUS}/src/fast/Fast3dWindow.cpp"
EDITS.append((F3D, [
    ("""bool Fast3dWindow::DrawAndRunGraphicsCommands(Gfx* commands, const std::unordered_map<Mtx*, MtxF>& mtxReplacements,
""", DECL + """#ifdef __IOS__
extern "C" void PBIos_SpkFrameBegin(void);
extern "C" void PBIos_SpkMark(int which);
extern "C" void PBIos_SpkFrameEnd(void);
#endif

bool Fast3dWindow::DrawAndRunGraphicsCommands(Gfx* commands, const std::unordered_map<Mtx*, MtxF>& mtxReplacements,
""", 1),
    ("""    auto gui = wnd->GetGui();
    // Setup mouse state manager
    wnd->GetMouseStateManager()->StartFrame();
    // Setup of the backend frames and draw initial Window and GUI menus
    gui->StartDraw();
    // Setup game framebuffers to match available window space
    mInterpreter->StartFrame();
    // Execute the games gfx commands
    mInterpreter->Run(commands, mtxReplacements, dlReplacements);
    // Renders the game frame buffer to the final window and finishes the GUI
    gui->EndDraw();
    // Finalize swap buffers
    mInterpreter->EndFrame();

    return true;
}

void Fast3dWindow::HandleEvents() {
    mWindowManagerApi->HandleEvents();
}
""", """#ifdef __IOS__
    PBIos_SpkFrameBegin(); // overlay 0041
#endif
    auto gui = wnd->GetGui();
    // Setup mouse state manager
    wnd->GetMouseStateManager()->StartFrame();
    // Setup of the backend frames and draw initial Window and GUI menus
    gui->StartDraw();
#ifdef __IOS__
    PBIos_SpkMark(0);
#endif
    // Setup game framebuffers to match available window space
    mInterpreter->StartFrame();
    // Execute the games gfx commands
    mInterpreter->Run(commands, mtxReplacements, dlReplacements);
#ifdef __IOS__
    PBIos_SpkMark(1);
#endif
    // Renders the game frame buffer to the final window and finishes the GUI
    gui->EndDraw();
#ifdef __IOS__
    PBIos_SpkMark(2);
#endif
    // Finalize swap buffers
    mInterpreter->EndFrame();
#ifdef __IOS__
    PBIos_SpkFrameEnd(); // after the present and LUS's pacing sleep
#endif

    return true;
}

void Fast3dWindow::HandleEvents() {
#ifdef __IOS__
    const unsigned long long pbSpkT0 = PBIos_SpkNow(); // overlay 0041: `ev`
#endif
    mWindowManagerApi->HandleEvents();
#ifdef __IOS__
    PBIos_SpkAdd(2, pbSpkT0);
#endif
}
""", 1),
]))

# --- gfx_metal.cpp ----------------------------------------------------------
METAL = f"{LUS}/src/fast/backends/gfx_metal.cpp"
EDITS.append((METAL, [
    ("""void GfxRenderingAPIMetal::NewFrame() {
    int width, height;
    SDL_GetRendererOutputSize(mRenderer, &width, &height);
    SetupScreenFramebuffer(width, height);
""", DECL + """#ifdef __IOS__
extern "C" void PBIos_PaceDrawableBegin(void);
extern "C" void PBIos_PaceDrawableEnd(void);
#endif

void GfxRenderingAPIMetal::NewFrame() {
    int width, height;
    SDL_GetRendererOutputSize(mRenderer, &width, &height);
#ifdef __IOS__
    PBIos_PaceDrawableBegin(); // overlay 0041: `wait` (nextDrawable)
#endif
    SetupScreenFramebuffer(width, height);
#ifdef __IOS__
    PBIos_PaceDrawableEnd();
#endif
""", 1),
    ("""void GfxRenderingAPIMetal::ReadFramebufferToCPU(int fb_id, uint32_t width, uint32_t height, uint16_t* rgba16_buf) {
""", """void GfxRenderingAPIMetal::ReadFramebufferToCPU(int fb_id, uint32_t width, uint32_t height, uint16_t* rgba16_buf) {
#ifdef __IOS__
    // overlay 0041: `fbrd` (the flame effect's readback waits on the GPU)
    struct PbSpkFbrd {
        unsigned long long t0;
        ~PbSpkFbrd() {
            PBIos_SpkAdd(1, t0);
        }
    } pbSpkFbrd{ PBIos_SpkNow() };
#endif
""", 1),
]))

# --- gfx_sdl2.cpp -----------------------------------------------------------
SDL2 = f"{LUS}/src/fast/backends/gfx_sdl2.cpp"
EDITS.append((SDL2, [
    ("""void GfxWindowBackendSDL2::SwapBuffersBegin() {
""", """#ifdef __IOS__
// PAPERBOAT_IOS (overlay 0041, D40): the frame's CPU work ends here and LUS's
// nanosleep pacer begins.
extern "C" void PBIos_PaceSleepBegin(void);
extern "C" void PBIos_PaceSleepEnd(void);
#endif

void GfxWindowBackendSDL2::SwapBuffersBegin() {
""", 1),
    ("""    SyncFramerateWithTime();
    SDL_GL_SwapWindow(mWnd);
""", """#ifdef __IOS__
    PBIos_PaceSleepBegin();
#endif
    SyncFramerateWithTime();
#ifdef __IOS__
    PBIos_PaceSleepEnd();
#endif
    SDL_GL_SwapWindow(mWnd);
""", 1),
]))

# --- Gui.cpp ----------------------------------------------------------------
GUI = f"{LUS}/src/ship/window/gui/Gui.cpp"
EDITS.append((GUI, [
    ("""#include "ship/window/gui/resource/GuiTexture.h"
""", """#include "ship/window/gui/resource/GuiTexture.h"

""" + DECL + """#ifdef __IOS__
// The menu-phase split (Shipwright 0068): time since the previous mark goes to
// the bucket; every GuiWindow is timed by name.
extern "C" void PBIos_MenuMark(int bucket);
extern "C" void PBIos_MenuWinBegin(void);
extern "C" void PBIos_MenuWinEnd(const char* name);
#endif
""", 1),
    ("""    if (GetMenuBar()) {
        GetMenuBar()->Update();
        GetMenuBar()->Draw();
    }

    if (GetMenu()) {
        GetMenu()->Update();
        GetMenu()->Draw();
    }

    for (auto& windowIter : mGuiWindows) {
        windowIter.second->Update();
        windowIter.second->Draw();
    }
""", """#ifdef __IOS__
    PBIos_MenuMark(3); // overlay 0041: dock (DrawMenu up to the menu bar)
#endif
    if (GetMenuBar()) {
        GetMenuBar()->Update();
        GetMenuBar()->Draw();
    }
#ifdef __IOS__
    PBIos_MenuMark(4); // bar
#endif

    if (GetMenu()) {
        GetMenu()->Update();
        GetMenu()->Draw();
    }
#ifdef __IOS__
    PBIos_MenuMark(5); // menu
#endif

    for (auto& windowIter : mGuiWindows) {
#ifdef __IOS__
        PBIos_MenuWinBegin();
#endif
        windowIter.second->Update();
        windowIter.second->Draw();
#ifdef __IOS__
        PBIos_MenuWinEnd(windowIter.first.c_str());
#endif
    }
#ifdef __IOS__
    PBIos_MenuMark(6); // win (every GuiWindow, incl. the SDL pump one)
#endif
""", 1),
    ("""void Gui::StartFrame() {
    HandleMouseCapture();
    ImGuiBackendNewFrame();
    ImGuiWMNewFrame();
    ImGui::NewFrame();
}
""", """void Gui::StartFrame() {
    HandleMouseCapture();
    ImGuiBackendNewFrame();
#ifdef __IOS__
    PBIos_MenuMark(0); // overlay 0041: nf (backend NewFrame; the probe removes the drawable wait)
#endif
    ImGuiWMNewFrame();
#ifdef __IOS__
    PBIos_MenuMark(1); // wm (ImGui SDL2 NewFrame)
#endif
    ImGui::NewFrame();
#ifdef __IOS__
    PBIos_MenuMark(2); // ig (ImGui::NewFrame)
#endif
}
""", 1),
    ("""    if (mNeedsConsoleVariableSave) {
        Ship::Context::GetRawInstance()->GetConsoleVariables()->Save();
        mNeedsConsoleVariableSave = false;
    }
""", """    if (mNeedsConsoleVariableSave) {
#ifdef __IOS__
        const unsigned long long pbSpkT0 = PBIos_SpkNow(); // overlay 0041: `cvsave`
#endif
        Ship::Context::GetRawInstance()->GetConsoleVariables()->Save();
#ifdef __IOS__
        PBIos_SpkAdd(5, pbSpkT0);
#endif
        mNeedsConsoleVariableSave = false;
    }
""", 1),
]))

# --- SDLAddRemoveDeviceEventHandler.cpp -------------------------------------
PUMP = f"{LUS}/src/ship/controller/physicaldevice/SDLAddRemoveDeviceEventHandler.cpp"
EDITS.append((PUMP, [
    ("""#include "ship/window/gui/Gui.h"
""", """#include "ship/window/gui/Gui.h"

""" + DECL, 1),
    ("""void SDLAddRemoveDeviceEventHandler::UpdateElement() {
    SDL_PumpEvents();
""", """void SDLAddRemoveDeviceEventHandler::UpdateElement() {
#ifdef __IOS__
    // overlay 0041: `pump` - on iOS this runs the UIKit run loop (touches, the
    // touch overlay's drawing, timers) inside the game frame, every frame.
    const unsigned long long pbSpkT0 = PBIos_SpkNow();
#endif
    SDL_PumpEvents();
#ifdef __IOS__
    PBIos_SpkAdd(3, pbSpkT0);
#endif
""", 1),
]))

# --- ResourceManager.cpp ----------------------------------------------------
RES = f"{LUS}/src/ship/resource/ResourceManager.cpp"
EDITS.append((RES, [
    ("""#include "ship/Context.h"
""", """#include "ship/Context.h"
""" + DECL, 1),
    ("""    const auto waitStart = std::chrono::steady_clock::now();
    auto resource = LoadResourceAsync(identifier, loadExact, BS::pr::highest, initData).get();
""", """#ifdef __IOS__
    // overlay 0041: `res` - a cache miss the caller waits on (main thread only).
    struct PbSpkRes {
        unsigned long long t0;
        ~PbSpkRes() {
            PBIos_SpkAdd(0, t0);
        }
    } pbSpkRes{ PBIos_SpkNow() };
#endif
    const auto waitStart = std::chrono::steady_clock::now();
    auto resource = LoadResourceAsync(identifier, loadExact, BS::pr::highest, initData).get();
""", 1),
]))

# --- Game.cpp ---------------------------------------------------------------
GAME = "src/port/Game.cpp"
EDITS.append((GAME, [
    ("""MtxF sInterpolationMatrixStack[0x1000];
""", DECL + """
MtxF sInterpolationMatrixStack[0x1000];
""", 1),
    ("""        OS_SiService();
""", """#ifdef __IOS__
        const unsigned long long pbSpkSi = PBIos_SpkNow(); // overlay 0041: `si`
#endif
        OS_SiService();
#ifdef __IOS__
        PBIos_SpkAdd(4, pbSpkSi);
#endif
""", 1),
    ("""            SDL_Delay(1);
        }
""", """#ifdef __IOS__
            const unsigned long long pbSpkIdle = PBIos_SpkNow(); // overlay 0041: `idle`
#endif
            SDL_Delay(1);
#ifdef __IOS__
            PBIos_SpkAdd(6, pbSpkIdle);
#endif
        }
""", 1),
]))


def udiff(rel_a, rel_b, a_text, b_text, new_file=False):
    with tempfile.TemporaryDirectory() as td:
        fa = pathlib.Path(td) / "a"
        fb = pathlib.Path(td) / "b"
        fa.write_text(a_text)
        fb.write_text(b_text)
        r = subprocess.run(["diff", "-u" + ("N" if new_file else ""), "--label", rel_a, "--label", rel_b,
                            str(fa), str(fb)], capture_output=True)
    assert r.returncode == 1, f"{rel_b}: diff produced no change"
    return r.stdout.decode()


chunks = []
new_rel = "src/port/ios/PBIosSpikeProbe.cpp"
assert not (VENDOR / new_rel).exists(), f"{new_rel} exists - reverse 0041 first"
chunks.append(udiff("/dev/null", f"b/{new_rel}", "", PROBE_SRC.read_text(), new_file=True))
for rel, subs in EDITS:
    orig = (VENDOR / rel).read_text()
    text = orig
    for old, new, expect in subs:
        n = text.count(old)
        assert n == expect, f"{rel}: expected {expect} match(es) of {old!r:.80}, got {n}"
        text = text.replace(old, new, 1)
    chunks.append(udiff(f"a/{rel}", f"b/{rel}", orig, text))

OUT.write_text(__doc__ + "\n" + "".join(chunks))
print(f"wrote {OUT.relative_to(ROOT)}")
