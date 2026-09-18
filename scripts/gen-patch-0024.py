#!/usr/bin/env python3
"""Overlay patch 0024 - the shell's unified touch layer drives the pad.

CLASS: (a) program-baseline parity.

REV 2 (round 17): an `#else` no-op for `TouchControls_ApplyShellPad`. The call
site in `GameEngine_ReadController()` is unconditional but rev 1 only DEFINED
the function inside `#if defined(PLATFORM_IOS) || defined(__IOS__)`, so the
macOS oracle did not link ("Undefined symbols for architecture arm64:
_TouchControls_ApplyShellPad"). It had gone unnoticed since round 14 because
nobody rebuilt the oracle; the spec says the oracle is green every round.
No iOS behaviour changes.

WHY
---
The user's verdict on 0.0.0.5: "the touch controls are COMPLETELY different than
what we have for other ports." Q-004's standing default (keep upstream's
engine-drawn pad, re-tuned to the unified table - D17, overlays 0013/0014) is
vetoed. The program's touch layer is a UIKit overlay that every one of the six
siblings carries, and matching its COORDINATES is not matching the CONTROL: the
floating stick's travel and knob, the 1.35x hit radii, slide-across between
buttons, the customizer's chrome and per-button eye chips are the shared thing,
and they live in `SohIosShell.m`, not in a table. (The siblings' Z double-tap
lock is the one deliberate omission - PM64 never holds Z; see D25.)

So `app/ios/PaperBoatIosShell.m` now carries that overlay (the design notes D25) and
this patch is its engine-side seam.

THE SEAM, AND WHY IT IS NOT THE SIBLINGS' VIRTUAL JOYSTICK
-----------------------------------------------------------
Every sibling injects through an SDL virtual joystick
(`SDL_JoystickSetVirtualAxis`). This port has a better seam already in the
tree: `GameEngine_ReadController` calls `controlDeck->WriteToPad(pads)` and then
upstream's own `TouchControls_ApplyPad(pads)`, which merges a touch pad's state
straight into `OSContPad`. One more call beside it does the same job for the
shell's layer, and it avoids three things the siblings pay for: SDL's joystick
lock on the audio/game path (a measured sibling audio-hitch cause), the
ControlDeck's "default mappings only when port 1 has no config" behaviour, and
the trigger half-press workaround Z needs on a virtual pad. Z here is the plain
CONT_G bit; the C-buttons are four real bits, not a right-stick emulation.

The `GamepadGameInputBlocked()` guard is mirrored from upstream's own merge
(:758-762) so that the bind-capture path still swallows game input.

UPSTREAM'S PAD IS DEFAULT-OFF ON iOS, IN THIS SAME PATCH
--------------------------------------------------------
`TOUCH_CONTROLS_DEFAULT` flips to 0 on iOS. Two pads at once is a half-working
pad: both would draw, both would merge, and a finger would press whichever one
hit-tested first. Upstream's is one checkbox away (Settings > Controls >
"Enable Touch Controls"), and the shell overlay stands down - draws nothing,
intercepts nothing - whenever `gTouchControls.Enabled` is 1, so the fallback is
a real fallback and not a fight. Android and desktop are untouched.

THE THREE STATE EXPORTS
-----------------------
`PBIos_IsMenuOpen` / `PBIos_IsPopupOpen` / `PBIos_ToggleMenu`. The overlay hides
itself and passes every touch through while the menu or a modal is up - the
INVERSE of the SoH port, because overlay 0016 reads menu swipes off ImGui's own
synthesised mouse and an overlay that swallowed them would kill menu scrolling
and the Torch first-run prompts alike. The menu chip calls ToggleVisibility()
directly and never injects an ESC key.

SAMPLING (the trap that makes a naive port drop taps)
------------------------------------------------------
`nuContDataGet[All]` reads the pad once per 30 Hz game tick, and
`GameEngine_ReadController` is called from several entry points per frame. The
shell therefore publishes a bit for ~60 ms after its release, so a fast tap
cannot fall between two ticks; latching on "consume" instead would hide the
press from every reader after the first in the same frame. That lives in the
shell; this file just reads the published word.

WHERE THE HUNKS SIT
-------------------
`TouchControls.cpp`: the includes + TOUCH_CONTROLS_DEFAULT at the very top
(~:1-24) and one block appended at EOF. Overlay 0013's hunks are at ~:112 and
in SafeRect/LoadPos, 0014's in ComputeLayout and the hideable table - hundreds
of lines from either end.
`TouchControls.h`: one declaration appended to the existing extern "C" block's
neighbourhood, clear of 0014's block.
`Engine.cpp`: one line inside `GameEngine_ReadController` (~:1533). Overlay
0012's hunks are at ~:1137+, 0022's at ~:357, 0023's at ~:994, 0004's at ~:190.

Match-count asserted against the pristine vendor state.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0024-paperboat-ios-shell-touch-pad.patch"

TC_REL = "src/port/ui/TouchControls.cpp"
TH_REL = "src/port/ui/TouchControls.h"
EN_REL = "src/port/Engine.cpp"


def replace_once(t, old, new, tag, rel):
    n = t.count(old)
    assert n == 1, f"{rel}: expected 1 match of {tag}, got {n}"
    return t.replace(old, new)


def unified(rel, orig, text):
    with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
         tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
        fa.write(orig); fb.write(text); fa.flush(); fb.flush()
        r = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}",
                            fa.name, fb.name], capture_output=True)
    assert r.returncode == 1, f"{rel}: diff produced no change"
    return r.stdout.decode()


# ---------------------------------------------------------------------------
# 1. TouchControls.h - the new entry point
# ---------------------------------------------------------------------------
th_src = VENDOR / TH_REL
th_orig = th_src.read_text()

# Appended at EOF, deliberately: overlay 0014's hunk in this file sits at
# pristine lines 10-16, and two patches that share context lines make the
# series non-idempotent (apply-overlay.sh probes "already applied" by
# reverse-applying at fuzz 0 - the spec trap).
OLD_H = """} // namespace PaperboatGui
#endif
"""
NEW_H = """} // namespace PaperboatGui
#endif

// PAPERBOAT_IOS (overlay 0024): merges the SHELL's UIKit touch overlay
// (app/ios/PaperBoatIosShell.m - the program's unified layer) into the same pad
// buffer, right after upstream's own TouchControls_ApplyPad(). Inert off iOS,
// and inert on iOS whenever the shell layer is not driving input.
#ifdef __cplusplus
extern "C" void TouchControls_ApplyShellPad(void* pads);
#else
void TouchControls_ApplyShellPad(void* pads);
#endif
"""
th = replace_once(th_orig, OLD_H, NEW_H, "ApplyShellPad declaration", TH_REL)

# ---------------------------------------------------------------------------
# 2. TouchControls.cpp - default off on iOS, and the merge itself
# ---------------------------------------------------------------------------
tc_src = VENDOR / TC_REL
tc_orig = tc_src.read_text()
tc = tc_orig

# Upstream's own #if block is left EXACTLY as it is and overridden immediately
# after it. Editing it in place would put this patch's context on the same lines
# as overlay 0014's include hunk (pristine 14-19), and two patches sharing
# context lines break apply-overlay.sh's reverse probe on the second build.
OLD_DEFAULT = """#else
#define TOUCH_CONTROLS_DEFAULT 0
#endif
"""

NEW_DEFAULT = """#else
#define TOUCH_CONTROLS_DEFAULT 0
#endif

#if defined(PLATFORM_IOS) || defined(__IOS__)
// PAPERBOAT_IOS (overlay 0024): upstream's pad is OFF by default on iOS. The
// app shell carries the program's unified UIKit touch layer and that is the pad
// the player gets; two pads at once is a half-working pad - both draw, both
// merge into OSContPad, and a finger presses whichever hit-tests first.
// Upstream's stays one checkbox away in Settings > Controls, and the shell
// layer stands down entirely (draws nothing, intercepts nothing) whenever
// gTouchControls.Enabled is 1, so the fallback is a real fallback. Android and
// desktop keep upstream's value.
#undef TOUCH_CONTROLS_DEFAULT
#define TOUCH_CONTROLS_DEFAULT 0
// GetTopMostPopupModal(), for the "is a modal on screen" export at the bottom
// of this file.
#include <imgui_internal.h>
#endif
"""

tc = replace_once(tc, OLD_DEFAULT, NEW_DEFAULT, "TOUCH_CONTROLS_DEFAULT", TC_REL)

OLD_TAIL = """} // namespace PaperboatGui
"""

NEW_TAIL = """} // namespace PaperboatGui

#if defined(PLATFORM_IOS) || defined(__IOS__)
// ---------------------------------------------------------------------------
// PAPERBOAT_IOS (overlay 0024): the app shell's unified touch layer.
//
// app/ios/PaperBoatIosShell.m draws a UIKit overlay - the same one every
// sibling port ships - and publishes its state as an N64 pad word plus a stick
// pair. This is where that state joins the game's input, one call after
// upstream's own TouchControls_ApplyPad() in GameEngine_ReadController().
//
// The shell answers 0 whenever its layer is not driving input (the CVar is off,
// upstream's pad is enabled instead, the menu or a modal is up, the customizer
// owns the screen), and then nothing here touches the pad at all.
// ---------------------------------------------------------------------------
extern "C" int PBIos_ShellPadState(uint16_t* buttons, int8_t* sx, int8_t* sy);

extern "C" void TouchControls_ApplyShellPad(void* pads) {
    uint16_t buttons = 0;
    int8_t sx = 0, sy = 0;
    if (!PBIos_ShellPadState(&buttons, &sx, &sy)) {
        return;
    }
    if (pads == nullptr) {
        return;
    }
    // Same guard as upstream's own merge (:758-762): while a binding is being
    // captured, or anything else asks for the pad to stay quiet, game input is
    // blocked - and a touch layer that ignored that would make the bind dialog
    // unusable on a phone.
    auto* ctx = Ship::Context::GetRawInstance();
    if (ctx == nullptr || ctx->GetControlDeck() == nullptr || ctx->GetControlDeck()->GamepadGameInputBlocked()) {
        return;
    }

    OSContPad* pad = static_cast<OSContPad*>(pads);
    pad->button |= buttons;
    // Only when nothing else is already deflecting the stick, exactly as
    // upstream's merge does: a physical pad in hand must always win.
    if ((sx != 0 || sy != 0) && pad->stick_x == 0 && pad->stick_y == 0) {
        pad->stick_x = sx;
        pad->stick_y = sy;
    }
}

// --- State the shell's overlay needs ---------------------------------------
//
// All three are called from the main thread (which is the game loop on iOS),
// from the overlay's 0.25 s sync timer and from its touch handlers. Each is
// null-safe end to end: the overlay is constructed before the engine exists.

extern "C" int PBIos_IsMenuOpen(void) {
    auto* ctx = Ship::Context::GetRawInstance();
    if (ctx == nullptr || ctx->GetWindow() == nullptr || ctx->GetWindow()->GetGui() == nullptr) {
        return 0;
    }
    const auto menu = ctx->GetWindow()->GetGui()->GetMenu();
    return (menu != nullptr && menu->IsVisible()) ? 1 : 0;
}

// Any ImGui modal: the Torch first-run prompts ("No O2R Files", "ROMs found",
// "Run PaperBoat?"), the extraction progress window, the input editor's
// dialogs. The overlay draws nothing and intercepts nothing while one is up -
// it would otherwise eat the taps that answer them, and its stick halo overlaps
// them square on.
extern "C" int PBIos_IsPopupOpen(void) {
    if (ImGui::GetCurrentContext() == nullptr) {
        return 0;
    }
    return ImGui::GetTopMostPopupModal() != nullptr ? 1 : 0;
}

// The menu chip. ToggleVisibility() directly - never a synthesised ESC key:
// this port's menu owns its own visibility and the key route would have to go
// through SDL, which is exactly the indirection the siblings regret.
extern "C" void PBIos_ToggleMenu(void) {
    auto* ctx = Ship::Context::GetRawInstance();
    if (ctx == nullptr || ctx->GetWindow() == nullptr || ctx->GetWindow()->GetGui() == nullptr) {
        return;
    }
    const auto menu = ctx->GetWindow()->GetGui()->GetMenu();
    if (menu != nullptr) {
        menu->ToggleVisibility();
    }
}
#else
// REV 2 (round 17): the macOS ORACLE has to link too. The call site in
// GameEngine_ReadController() is unconditional - deliberately, so the input
// path reads the same on every platform - so the symbol must exist everywhere,
// and off iOS there is no shell to ask. A no-op, and the only thing the oracle
// ever does with it.
//
// Found by scripts/build-oracle.sh failing with "Undefined symbols ...
// _TouchControls_ApplyShellPad" on the first oracle build after this overlay
// landed (round 14). The spec's rule is that the oracle is green on every
// round; nobody had rebuilt it since.
extern "C" void TouchControls_ApplyShellPad(void* pads) {
    (void)pads;
}
#endif // PLATFORM_IOS || __IOS__
"""
tc = replace_once(tc, OLD_TAIL, NEW_TAIL, "end of namespace PaperboatGui", TC_REL)

# ---------------------------------------------------------------------------
# 3. Engine.cpp - the call site
# ---------------------------------------------------------------------------
en_src = VENDOR / EN_REL
en_orig = en_src.read_text()

OLD_ENGINE = """    // Merges the on-screen controls into port 0; no-op unless enabled.
    TouchControls_ApplyPad(pads);
}
"""

NEW_ENGINE = """    // Merges the on-screen controls into port 0; no-op unless enabled.
    TouchControls_ApplyPad(pads);
    // PAPERBOAT_IOS (overlay 0024): and the app shell's unified UIKit touch
    // layer, which is the pad iOS actually ships with. No-op off iOS, and
    // no-op on iOS whenever that layer is not driving input.
    TouchControls_ApplyShellPad(pads);
}
"""
en = replace_once(en_orig, OLD_ENGINE, NEW_ENGINE, "GameEngine_ReadController touch merge", EN_REL)

OUT.write_text(__doc__ + "\n"
               + unified(TH_REL, th_orig, th)
               + unified(TC_REL, tc_orig, tc)
               + unified(EN_REL, en_orig, en))
print(f"wrote {OUT}")
