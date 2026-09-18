// PaperBoatIosConsole.cpp — the ONLY file of the port's iOS shell that speaks
// libultraship. Everything else in app/ios/ is plain Objective-C.
//
// WHY A SEPARATE FILE
// -------------------
// The remote console bridge (round 4, overlay 0006) has to reach LUS's command
// interpreter (`Ship::Console::Run`), which is C++ with std::string/std::vector
// in its signature, and the settings flush has to reach `ConsoleVariable::Save()`.
// PaperBoatIosShell.m is Objective-C, not Objective-C++, and the cheapest way to
// keep it that way is a small C++ translation unit exporting a C ABI:
//
//     int  PBIos_EngineReady(void);
//     int  PBIos_ConsoleRun(const char* line, char* out, int cap);
//     int  PBIos_ConfigSave(void);
//
// The alternative — renaming the shell to .mm — would recompile 200+ lines of
// UIKit code as Objective-C++ to gain three calls, and would put every LUS
// header (and its ImGui dependency) into the same translation unit as the
// UIWindow swizzle. Kept apart, a LUS header change can only break this file.
//
// THREADING. Nothing here takes a lock. Every entry point is expected to be
// called on the MAIN thread, which on iOS is the thread SDL_main (and therefore
// the whole game loop) runs on: the bridge's socket thread hops onto the main
// queue and waits for the result. LUS's CVar map is not thread-safe (upstream
// has no locking at all — that is SoH port overlay 0030, still unported here),
// so calling Console::Run from the socket thread would be a concurrent
// find()/rehash on an unordered_map, i.e. an eventual SIGSEGV that looks like a
// game bug. See docs/console-bridge.md.

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "ship/Context.h"
#include "ship/config/Config.h"
#include "ship/config/ConsoleVariable.h"
#include "ship/debug/Console.h"

namespace {

Ship::Context* PBCtx() {
    return Ship::Context::GetRawInstance();
}

// "Ready" is deliberately strict: a Context exists from very early in
// GameEngine's constructor, but its console has no commands until Gui::Init()
// runs ConsoleWindow::InitElement (that is where "set"/"get" are registered).
// Answering "ready" before then would let the bridge run `set` into a console
// that silently has no such command.
bool PBReady() {
    Ship::Context* ctx = PBCtx();
    if (ctx == nullptr) {
        return false;
    }
    std::shared_ptr<Ship::Console> console = ctx->GetConsole();
    if (console == nullptr || ctx->GetConsoleVariables() == nullptr || ctx->GetConfig() == nullptr) {
        return false;
    }
    return console->HasCommand("set");
}

// `crashtest`, registered on the port's side rather than upstream's: LUS
// installs NO signal handler on Apple platforms (CrashHandler.cpp's
// implementation is `#if defined(__linux__) && !defined(__ANDROID__)` / `_WIN32`
// only), so the shell installs its own (overlay round 4, Documents/crash.txt)
// and this is the button that proves it fires. A null dereference rather than
// abort(): it is the crash shape the renderer and the resource manager actually
// produce, and it arrives as SIGSEGV, the signal a handler is most likely to
// get wrong.
void PBRegisterCommandsOnce() {
    static bool registered = false;
    if (registered) {
        return;
    }
    std::shared_ptr<Ship::Console> console = PBCtx()->GetConsole();
    if (console->HasCommand("crashtest")) {
        registered = true;
        return;
    }
    console->AddCommand("crashtest",
                        { [](std::shared_ptr<Ship::Console>, std::vector<std::string>, std::string*) -> int32_t {
                             // volatile so the optimiser cannot decide this is
                             // UB it may delete; the point IS the fault.
                             volatile int* nowhere = nullptr;
                             *nowhere = 0x50424f41; // "PBOA"
                             return 0;
                         },
                          "Deliberately crash the app (proves Documents/crash.txt)" });
    registered = true;
}

} // namespace

extern "C" int PBIos_EngineReady(void) {
    return PBReady() ? 1 : 0;
}

// Runs one console line and copies the interpreter's output into `out`.
// Returns the command's own return code (0 = success), or -1 when the engine is
// not up yet, or -2 when no such command is registered — Console::Run cannot
// distinguish "handler returned 0" from "no such command" (both are 0), so the
// lookup happens here.
extern "C" int PBIos_ConsoleRun(const char* line, char* out, int cap) {
    if (out != nullptr && cap > 0) {
        out[0] = '\0';
    }
    if (line == nullptr || line[0] == '\0') {
        return -2;
    }
    if (!PBReady()) {
        if (out != nullptr && cap > 0) {
            std::snprintf(out, (size_t)cap, "engine not ready (console has no commands yet)");
        }
        return -1;
    }
    PBRegisterCommandsOnce();

    std::string command(line);
    // Console::Run splits on ' ' only; a trailing \r (telnet, nc -C) would
    // become part of the last argument and turn `set gX 1` into `set gX 1\r`.
    while (!command.empty() && (command.back() == '\n' || command.back() == '\r' || command.back() == ' ')) {
        command.pop_back();
    }
    if (command.empty()) {
        return -2;
    }

    const std::string name = command.substr(0, command.find(' '));
    std::shared_ptr<Ship::Console> console = PBCtx()->GetConsole();
    if (!console->HasCommand(name)) {
        if (out != nullptr && cap > 0) {
            std::snprintf(out, (size_t)cap, "no such command: %s", name.c_str());
        }
        return -2;
    }

    std::string output;
    const int32_t rc = console->Run(command, &output);
    if (out != nullptr && cap > 0) {
        std::snprintf(out, (size_t)cap, "%s", output.c_str());
    }
    return (int)rc;
}

// CVar reads for the Objective-C half of the shell (round 11). The shell owns
// the iOS-only defaults the menu's Settings > iOS page edits — the SSAA factor
// (gSohIos.Supersample) and the remote-console opt-in (gSohIos.RemoteConsole) —
// and PaperBoatIosShell.m cannot reach LUS's CVar table itself without becoming
// Objective-C++ (see the header comment). Two lines here instead.
//
// Both answer the caller's default until the engine is up, so a caller during
// launch gets the same answer it would get from an unset CVar rather than a
// crash. Safe on the main thread; overlay 0011 put a recursive_mutex on the
// table, so a stray off-thread read is no longer a rehash race either.
extern "C" float PBIos_CVarGetFloat(const char* name, float defaultValue) {
    Ship::Context* ctx = PBCtx();
    if (ctx == nullptr || ctx->GetConsoleVariables() == nullptr || name == nullptr) {
        return defaultValue;
    }
    return ctx->GetConsoleVariables()->GetFloat(name, defaultValue);
}

extern "C" int PBIos_CVarGetInt(const char* name, int defaultValue) {
    Ship::Context* ctx = PBCtx();
    if (ctx == nullptr || ctx->GetConsoleVariables() == nullptr || name == nullptr) {
        return defaultValue;
    }
    return (int)ctx->GetConsoleVariables()->GetInteger(name, (int32_t)defaultValue);
}

// Synchronous settings write, for the resign-active flush (overlay round 4).
// ConsoleVariable::Save() copies every live CVar into the Config and calls
// Config::Save(), which writes paperboat.cfg.json through a .tmp + rename —
// that is the whole persisted settings state of this port (the touch layout,
// the controller map, the window backend). It is ~13 KB and takes under a
// millisecond; iOS gives a backgrounding app seconds, not milliseconds.
//
// Game saves are NOT flushed here and deliberately so: src/port/save/SaveManager.cpp
// writes file<N>.json inside the OnSaveFileSave event handler and closes the
// stream there, so a save is already on disk the moment the game performs it.
// There is no write-on-quit path for saves to lose.
extern "C" int PBIos_ConfigSave(void) {
    Ship::Context* ctx = PBCtx();
    if (ctx == nullptr) {
        return 0;
    }
    std::shared_ptr<Ship::ConsoleVariable> cvars = ctx->GetConsoleVariables();
    if (cvars == nullptr || ctx->GetConfig() == nullptr) {
        return 0;
    }
    cvars->Save();
    return 1;
}

// CVar WRITES for the Objective-C half of the shell (round 12, the unified
// touch layer). The overlay's layout customizer persists its state in the
// family's `gSohIos.Layout.*` block, and the shell cannot reach LUS's CVar
// table itself without becoming Objective-C++ (see the header comment).
//
// Same contract as the readers above: a call before the engine is up is a
// silent no-op rather than a crash, so the overlay can be constructed at any
// lifecycle point. Main thread, like everything else here; overlay 0011's
// recursive_mutex covers the table either way.
extern "C" void PBIos_CVarSetFloat(const char* name, float value) {
    Ship::Context* ctx = PBCtx();
    if (ctx == nullptr || ctx->GetConsoleVariables() == nullptr || name == nullptr) {
        return;
    }
    ctx->GetConsoleVariables()->SetFloat(name, value);
}

extern "C" void PBIos_CVarSetInt(const char* name, int value) {
    Ship::Context* ctx = PBCtx();
    if (ctx == nullptr || ctx->GetConsoleVariables() == nullptr || name == nullptr) {
        return;
    }
    ctx->GetConsoleVariables()->SetInteger(name, (int32_t)value);
}

// Used by the customizer's Save: stale per-button keys from an earlier save
// were resurrecting on the next launch on the siblings (Lighthouse's
// "deterministic jumble" device bug), so the whole block is cleared before the
// live set is written back.
extern "C" void PBIos_CVarClearBlock(const char* name) {
    Ship::Context* ctx = PBCtx();
    if (ctx == nullptr || ctx->GetConsoleVariables() == nullptr || name == nullptr) {
        return;
    }
    ctx->GetConsoleVariables()->ClearBlock(name);
}

// The customizer's Save writes to disk immediately: an iOS swipe-kill is
// SIGKILL and a layout the player just tuned must not depend on the
// resign-active flush having run.
extern "C" void PBIos_CVarSave(void) {
    (void)PBIos_ConfigSave();
}

// ---------------------------------------------------------------------------
// ROUND 22 — PHYSICAL GAME CONTROLLERS (parity checklist row 19).
//
// THE FINDING FIRST, because it decides what this code is. This port needs NO
// native GCController translation, and neither does any sibling: SDL2 is built
// here with SDL_JOYSTICK_MFI=1 and GameController.framework on the link line
// (build-sim/_deps/sdl2-build/include-config-*/SDL2/SDL_config.h:346), so an
// MFi/Xbox/DualSense pad arrives as an SDL_GameController; LUS opens it
// (ConnectedPhysicalDeviceManager), maps it with the family's own default N64
// layout (LUS::ControllerDefaultMappings: A->A, B->B, L->LB, Start->Start,
// D-pad->D-pad, Z->left trigger, R->right trigger, C->right stick at a 25 %
// threshold), blocks it while a bind is being captured
// (GamepadGameInputBlocked) and hands ImGui the pad while the menu is open if
// the user ticks Controller Navigation. Writing a second translation layer
// beside that one would double every bit. The design notes D33.
//
// So what is missing is not translation — it is EVIDENCE, on a machine with no
// pad in it. This block is the injection harness the family uses for hardware
// it does not have (the Sense-controller `vr hand` verb is the same shape): an
// SDL VIRTUAL game controller, attached on demand, driven from the bridge. It
// enters the engine through exactly the path a real pad does — SDL device-added
// event, LUS port assignment, default mappings, SDLButtonToButtonMapping —
// which is the point: nothing here is a test double for the thing under test.
//
// The injection half is compiled out of release builds with the rest of the
// bridge; PBIos_PadConnectedCount is NOT, because the touch layer yields to a
// connected pad and that behaviour must not differ between build flavours.
// ---------------------------------------------------------------------------
#include <SDL2/SDL.h>

#include "libultraship/libultra/controller.h"
#include "ship/controller/controldeck/ControlDeck.h"
#include "ship/controller/controldevice/controller/Controller.h"
#include "ship/window/gui/Gui.h"

// Every SDL gamepad the engine can see. Also what the shell's touch layer
// yields to (PBIos_PhysicalControllerPresent ORs it in), so the number is
// load-bearing, not decoration.
extern "C" int PBIos_PadConnectedCount(void) {
    if (SDL_WasInit(SDL_INIT_GAMECONTROLLER) == 0) {
        return 0;
    }
    int n = 0;
    for (int i = 0; i < SDL_NumJoysticks(); i++) {
        if (!SDL_IsGameController(i)) {
            continue;
        }
        // The same filter the GCController probe applies, for the same reason
        // and measured on the same machine: the SIMULATOR synthesises a generic
        // keyboard-passthrough pad named exactly "Gamepad", and it reaches SDL
        // too (sdl_pads=1 names=['Gamepad'] on a sim with nothing attached).
        // Without this the touch layer yields to a pad nobody is holding, on
        // every simulator run. Real hardware reports its brand.
        const char* name = SDL_GameControllerNameForIndex(i);
        if (name != nullptr && std::strcmp(name, "Gamepad") == 0) {
            continue;
        }
        n++;
    }
    return n;
}

#if PAPERBOAT_REMOTE_CONSOLE

namespace {

SDL_Joystick* gPbVirtualPad = nullptr;
int gPbVirtualPadIndex = -1;

struct PbPadName {
    const char* name;
    int sdlButton;                 // SDL_CONTROLLER_BUTTON_*, or -1 for an axis
    SDL_GameControllerAxis axis;   // used when sdlButton < 0
    int axisSign;                  // +1 / -1
};

// The names are the N64 button's, because that is what a tester means; the
// element each one drives is the one LUS's DEFAULT mapping listens to, so this
// table is an assertion about the default layout as much as a lookup.
const PbPadName kPbPadNames[] = {
    { "a", SDL_CONTROLLER_BUTTON_A, SDL_CONTROLLER_AXIS_INVALID, 0 },
    { "b", SDL_CONTROLLER_BUTTON_B, SDL_CONTROLLER_AXIS_INVALID, 0 },
    { "l", SDL_CONTROLLER_BUTTON_LEFTSHOULDER, SDL_CONTROLLER_AXIS_INVALID, 0 },
    { "start", SDL_CONTROLLER_BUTTON_START, SDL_CONTROLLER_AXIS_INVALID, 0 },
    { "du", SDL_CONTROLLER_BUTTON_DPAD_UP, SDL_CONTROLLER_AXIS_INVALID, 0 },
    { "dd", SDL_CONTROLLER_BUTTON_DPAD_DOWN, SDL_CONTROLLER_AXIS_INVALID, 0 },
    { "dl", SDL_CONTROLLER_BUTTON_DPAD_LEFT, SDL_CONTROLLER_AXIS_INVALID, 0 },
    { "dr", SDL_CONTROLLER_BUTTON_DPAD_RIGHT, SDL_CONTROLLER_AXIS_INVALID, 0 },
    { "z", -1, SDL_CONTROLLER_AXIS_TRIGGERLEFT, 1 },
    { "r", -1, SDL_CONTROLLER_AXIS_TRIGGERRIGHT, 1 },
    { "cu", -1, SDL_CONTROLLER_AXIS_RIGHTY, -1 },
    { "cd", -1, SDL_CONTROLLER_AXIS_RIGHTY, 1 },
    { "cl", -1, SDL_CONTROLLER_AXIS_RIGHTX, -1 },
    { "cr", -1, SDL_CONTROLLER_AXIS_RIGHTX, 1 },
};

// A released trigger is AXIS_MIN, not 0. SDL maps a full-range axis into
// trigger space as (raw + 32768) / 2, so a raw 0 reads as 16383 — above LUS's
// 25 % press threshold, i.e. Z held down forever. This is the family's most
// expensive controller bug (SoH port PORTING.md "mandatory fix 1"); it is
// re-paid here because a virtual pad's axes start at 0.
void PbReleaseTriggers() {
    if (gPbVirtualPad == nullptr) {
        return;
    }
    SDL_JoystickSetVirtualAxis(gPbVirtualPad, SDL_CONTROLLER_AXIS_TRIGGERLEFT, SDL_JOYSTICK_AXIS_MIN);
    SDL_JoystickSetVirtualAxis(gPbVirtualPad, SDL_CONTROLLER_AXIS_TRIGGERRIGHT, SDL_JOYSTICK_AXIS_MIN);
}

} // namespace

// Attach the diagnostic pad. Idempotent; returns 1 when one is attached.
extern "C" int PBIos_PadAttach(void) {
    if (gPbVirtualPad != nullptr) {
        return 1;
    }
    if (SDL_WasInit(SDL_INIT_GAMECONTROLLER) == 0) {
        return 0; // the engine has not initialised SDL yet
    }
    const int index = SDL_JoystickAttachVirtual(SDL_JOYSTICK_TYPE_GAMECONTROLLER, SDL_CONTROLLER_AXIS_MAX,
                                                SDL_CONTROLLER_BUTTON_MAX, 1);
    if (index < 0) {
        return 0;
    }
    gPbVirtualPadIndex = index;
    gPbVirtualPad = SDL_JoystickOpen(index);
    if (gPbVirtualPad == nullptr) {
        SDL_JoystickDetachVirtual(index);
        gPbVirtualPadIndex = -1;
        return 0;
    }
    PbReleaseTriggers();
    // LUS adopts the device from the SDL event queue, which the game loop pumps
    // once per frame (GameEngine::StartFrame -> Window::HandleEvents).
    SDL_PumpEvents();
    return 1;
}

extern "C" int PBIos_PadDetach(void) {
    if (gPbVirtualPad == nullptr) {
        return 0;
    }
    SDL_JoystickClose(gPbVirtualPad);
    gPbVirtualPad = nullptr;
    if (gPbVirtualPadIndex >= 0) {
        SDL_JoystickDetachVirtual(gPbVirtualPadIndex);
        gPbVirtualPadIndex = -1;
    }
    SDL_PumpEvents();
    return 1;
}

// One N64 button by name, pressed or released, on the diagnostic pad.
extern "C" int PBIos_PadButton(const char* name, int down) {
    if (gPbVirtualPad == nullptr || name == nullptr) {
        return 0;
    }
    for (const PbPadName& b : kPbPadNames) {
        if (std::strcmp(b.name, name) != 0) {
            continue;
        }
        if (b.sdlButton >= 0) {
            SDL_JoystickSetVirtualButton(gPbVirtualPad, b.sdlButton, down ? SDL_PRESSED : SDL_RELEASED);
        } else if (b.axis == SDL_CONTROLLER_AXIS_TRIGGERLEFT || b.axis == SDL_CONTROLLER_AXIS_TRIGGERRIGHT) {
            SDL_JoystickSetVirtualAxis(gPbVirtualPad, b.axis, down ? SDL_JOYSTICK_AXIS_MAX : SDL_JOYSTICK_AXIS_MIN);
        } else {
            SDL_JoystickSetVirtualAxis(gPbVirtualPad, b.axis,
                                       down ? (Sint16)(b.axisSign > 0 ? SDL_JOYSTICK_AXIS_MAX : SDL_JOYSTICK_AXIS_MIN)
                                            : (Sint16)0);
        }
        SDL_PumpEvents();
        return 1;
    }
    return 0;
}

// The left or right stick, in [-1, 1]. y is screen-down-positive on SDL, which
// is the same convention the engine's mappings expect.
extern "C" int PBIos_PadStick(const char* which, float x, float y) {
    if (gPbVirtualPad == nullptr || which == nullptr) {
        return 0;
    }
    const bool right = (which[0] == 'r' || which[0] == 'R');
    const SDL_GameControllerAxis ax = right ? SDL_CONTROLLER_AXIS_RIGHTX : SDL_CONTROLLER_AXIS_LEFTX;
    const SDL_GameControllerAxis ay = right ? SDL_CONTROLLER_AXIS_RIGHTY : SDL_CONTROLLER_AXIS_LEFTY;
    auto clamp = [](float v) { return v < -1.0f ? -1.0f : (v > 1.0f ? 1.0f : v); };
    SDL_JoystickSetVirtualAxis(gPbVirtualPad, ax, (Sint16)(clamp(x) * 32767.0f));
    SDL_JoystickSetVirtualAxis(gPbVirtualPad, ay, (Sint16)(clamp(y) * 32767.0f));
    SDL_PumpEvents();
    return 1;
}

// The state of the whole controller path in one line: what SDL sees, what the
// engine's port 1 makes of it RIGHT NOW (the same ReadToPad the game calls),
// and the two predicates that decide whether the game may have it at all.
extern "C" int PBIos_PadReport(char* out, int cap) {
    if (out == nullptr || cap <= 0) {
        return 0;
    }
    // All four ports, because WHICH port a newly connected pad lands on is a
    // real open question in this family (the SoH port's MEASUREMENTS note on
    // hot-plug), and on a simulator port 1 is already taken by the synthetic
    // "Gamepad". Port 1 is pads[0] and is the one the game plays from.
    OSContPad pad[4] = {};
    int blocked = -1;
    int menu = -1;
    int ports = 0;
    Ship::Context* ctx = PBCtx();
    if (ctx != nullptr && ctx->GetControlDeck() != nullptr) {
        blocked = ctx->GetControlDeck()->GamepadGameInputBlocked() ? 1 : 0;
        for (uint8_t i = 0; i < 4; i++) {
            std::shared_ptr<Ship::Controller> controller = ctx->GetControlDeck()->GetControllerByPort(i);
            if (controller != nullptr) {
                ports |= (1 << i);
                // Not ControlDeck::WriteToPad: that stores the pad POINTER in
                // the deck (mPads = pad), and this one is on our stack.
                controller->ReadToPad(&pad[i]);
            }
        }
    }
    if (ctx != nullptr && ctx->GetWindow() != nullptr && ctx->GetWindow()->GetGui() != nullptr) {
        menu = ctx->GetWindow()->GetGui()->GetMenuOrMenubarVisible() ? 1 : 0;
    }

    char names[192] = "";
    int used = 0;
    const int sdlPads = PBIos_PadConnectedCount();
    for (int i = 0; i < SDL_NumJoysticks() && used < (int)sizeof(names) - 32; i++) {
        if (!SDL_IsGameController(i)) {
            continue;
        }
        const char* n = SDL_GameControllerNameForIndex(i);
        used += std::snprintf(names + used, sizeof(names) - used, "%s'%s'", used ? "," : "", n ? n : "?");
    }

    return std::snprintf(out, (size_t)cap,
                         "virtual=%d sdl_pads=%d names=[%s] ports=0x%x pad=0x%04x stick=%d,%d "
                         "pads=0x%04x,0x%04x,0x%04x,0x%04x menu=%d blocked=%d nav=%d",
                         gPbVirtualPad != nullptr ? 1 : 0, sdlPads, names, ports, (unsigned)pad[0].button,
                         (int)pad[0].stick_x, (int)pad[0].stick_y, (unsigned)pad[0].button,
                         (unsigned)pad[1].button, (unsigned)pad[2].button, (unsigned)pad[3].button, menu, blocked,
                         PBIos_CVarGetInt("gSettings.ControlNav", 0));
}

#endif // PAPERBOAT_REMOTE_CONSOLE
