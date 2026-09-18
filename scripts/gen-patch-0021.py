#!/usr/bin/env python3
"""Overlay patch 0021 - the iOS settings section (Settings > iOS) and the
performance HUD.

CLASS: (a) program-baseline parity. Cross-port adoption of Lighthouse's overlay
0017, rendered in PaperBoat's own menu framework (same SoH lineage:
AddSidebarEntry / AddWidget / WidgetPath / *Options builders), so it ports by
idiom rather than by rewrite.

SECTIONS AND KNOBS
------------------
  Touch Controls - Customize Layout... , Touch Control Opacity, Haptic
                   Feedback, Stick Response, Left-Handed Layout
  Menu           - Menu Scale            (engine half: overlay 0022)
  Display        - Supersampling         (engine half: overlay 0019)
                   Max Frame Rate        (engine half: overlay 0023)
                   Performance HUD       (drawn here, data from overlay 0012)
  Advanced       - Async Shaders         (engine half: overlay 0027)
                   Audio Buffer          (engine half: overlay 0026)
                   Remote Console        (gated on PAPERBOAT_REMOTE_CONSOLE)

CVAR BLOCK: `gSohIos.*`. That is the family-wide block every sibling uses, kept
verbatim so the harbour-shell extraction has nothing to rename; the port's own
upstream CVars keep their upstream names (`gTouchControls.*`). Recorded in
the shell divergence notes.

REVISED IN ROUND 12 (the unified touch layer, overlay 0024 / the design notes D25)
---------------------------------------------------------------------------
The pad on screen is no longer upstream's - it is the program's UIKit layer in
`app/ios/PaperBoatIosShell.m`, the same one every sibling ships. Four knobs in
this section changed owner with it:

* **Customize Layout...** now sets `gSohIos.EditLayout`, the SHELL's transient
  trigger, instead of upstream's `gTouchControls.EditMode`. The shell's editor
  is the one that can move, scale and hide the chips that are actually drawn.
* **Touch Control Opacity** binds `gSohIos.TouchOpacity`, not upstream's
  `gTouchControls.Opacity`. It dims the shell's layer, which cannot read an
  engine CVar without becoming an engine client. Upstream's slider stays in
  Settings > Controls, where upstream's own pad still lives.
* **Haptics, Stick Response and Left-Handed Layout** (checklist rows 8, 10, 11)
  arrive now because the shell finally reads them: `hapticTap` on button down,
  the expo curve in `PBIos_StickValue`, and `applyLefty:` across the layout.
  They were deliberately absent before for exactly this reason.
* **No Visible Buttons list.** The shell's customizer has a per-button eye chip,
  which is the family's mechanism; two ways to hide the same button is a
  settings page that contradicts itself. Overlay 0014's engine-side list stays
  `#ifndef __IOS__` in Controls, for upstream's pad on desktop.

And upstream's own "Enable Touch Controls" checkbox in Settings > Controls gets
an iOS-only tooltip saying what it now does: it REPLACES the iOS layer (overlay
0024 flips `TOUCH_CONTROLS_DEFAULT` to 0 on iOS and the shell layer stands down
while that CVar is set), so only one pad is ever live.

Still not here: Async Shader Compilation (checklist row 13). A knob that does
nothing is worse than a missing knob. Double-Tap Z is not here either and never
will be: the gesture itself was deleted from the shell (the user, 2026-09-17 -
PM64 never needs Z held), so there is no CVar left to expose.

THE PERFORMANCE HUD
-------------------
`gSohIos.PerfHud` draws the Phase 0.3 harness's numbers in the top-right
corner, inside the safe rect (`PBIos_GetSafeAreaInsets`, overlay 0013, drawable
pixels - D16). It is a tiny always-registered `Ship::GuiWindow` that overrides
`Draw()` and paints straight into ImGui's foreground draw list, exactly as
upstream's own `TouchControlsOverlay` does; that keeps it out of the menu's
lifetime and off every other file in the series.

ROUND 22: the HUD reads as SoH's does - the whole-number frame rate ALONE, no
"fps" word and no frame time, with ` - warm` / ` - HOT` appended at thermal 1
and 2+ (`SohIosShell.m` `SohIos_PerfHud3DText`, the three cases verbatim).
The user asked for the family's HUDs to be the same glance; the frame time lives
on the bridge's `fps` line, which is where a number worth reading twice belongs.

It formats itself out of `PBIos_PerfReport` (overlay 0012) rather than reaching
into the probe: the probe's rings and its mutex stay owned by one file, and the
bridge's `fps` verb and the HUD are then provably showing the same numbers.
Parsing our own one-line format is the price, and it is three `strstr` calls.

TRAPS OBSERVED
--------------
* `ComboMap` here takes `std::unordered_map<int32_t, const char*>` (checked
  against UIWidgets.hpp:304 - same as Lighthouse, unlike the SoH port's
  `std::map`), and `Combobox` stores the map KEY in the CVar
  (`comboMap.at(*value)`, UIWidgets.hpp:690).
* **Max Frame Rate is VALUE-keyed** ({60,120}, default 120) because overlay
  0023 reads the CVar as a real frame rate. An index-keyed map would write 0/1
  and clamp the engine to 0 fps. `comboMap.at()` throws on a key that is not in
  the map, so a value-keyed map is only safe when every writable value is a key
  - which it is here, and the default (120) is one of them.
* ASCII only in widget strings: the menu's font atlas has no em-dash or bullet
  and renders them as '?'.
* Defaults are read from the shell (app/ios/PaperBoatIosShell.m), never
  invented here: Supersample 0.0 -> the shell's device tier, MaxFps 120,
  PerfHud 0, RemoteConsole 0, MenuScale 0.85.

WHERE THE HUNKS SIT
-------------------
`PaperboatMenuSettings.cpp`: the combo maps at the top (after the file's other
static maps, ~:47), the section itself immediately before "// Settings > Audio"
(~:146), and the upstream touch checkbox's tooltip (~:296). Overlay 0014's hunk
in this file is at ~:253 and overlay 0020's at ~:300 - the tooltip hunk is
checked against both after every regeneration. `PaperboatGui.cpp` is touched by nothing else in the series.

Match-count asserted against the pristine vendor state.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0021-paperboat-ios-settings-section.patch"

MENU_REL = "src/port/ui/PaperboatMenuSettings.cpp"
GUI_REL = "src/port/ui/PaperboatGui.cpp"


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
# 1. PaperboatMenuSettings.cpp
# ---------------------------------------------------------------------------
menu_src = VENDOR / MENU_REL
menu_orig = menu_src.read_text()
menu = menu_orig

OLD_MAPS = """static const std::unordered_map<int32_t, const char*> notificationPosition = {
    { 0, "Top Left" }, { 1, "Top Right" }, { 2, "Bottom Left" }, { 3, "Bottom Right" }, { 4, "Hidden" },
};
"""

NEW_MAPS = """static const std::unordered_map<int32_t, const char*> notificationPosition = {
    { 0, "Top Left" }, { 1, "Top Right" }, { 2, "Bottom Left" }, { 3, "Bottom Right" }, { 4, "Hidden" },
};

#ifdef __IOS__
// PAPERBOAT_IOS (overlay 0021): VALUE-keyed on purpose, unlike every other map
// in this file. Combobox stores the map KEY in the CVar, and overlay 0023 reads
// gSohIos.MaxFps as an actual frame rate in GetInterpolationFPS(); an
// index-keyed map here would write 0/1 and clamp the engine to 0 fps. Safe
// against comboMap.at()'s throw because the only values ever written are these
// two keys, and the default (120) is one of them.
static const std::unordered_map<int32_t, const char*> iosMaxFpsOptions = {
    { 60, "60 FPS" },
    { 120, "120 FPS" },
};

// INDEX-keyed, like every other map in this file and unlike the one above:
// the shell reads these as modes, not as measurements.
static const std::unordered_map<int32_t, const char*> iosHapticsOptions = {
    { 0, "Off" },
    { 1, "Light" },
    { 2, "Strong" },
};

static const std::unordered_map<int32_t, const char*> iosStickCurveOptions = {
    { 0, "Linear" },
    { 1, "Precise" },
};

// PAPERBOAT_IOS (overlay 0021 rev 3): the app shell owns every iOS default, and
// the SSAA one is a DEVICE TIER, not a number (Apple8 -> 2.0, Apple7 -> 1.5,
// else 1.0 - app/ios/PaperBoatIosShell.m, the design notes D23). The slider below
// hands it straight to DefaultValue so the widget reads the factor the engine
// is actually rendering at, instead of a hardcoded 1.00x that contradicts it.
// UIWidgets reads a slider as CVarGetFloat(cvar, options.defaultValue), so this
// writes nothing to the config until the player moves the slider.
extern "C" float PBIos_SsaaDefaultValue(void);
#endif
"""
menu = replace_once(menu, OLD_MAPS, NEW_MAPS, "combo maps", MENU_REL)

OLD_FALLBACK = """    AddWidget(path, "Enable Touch Controls", WIDGET_CVAR_CHECKBOX)
        .CVar(CVAR_TOUCH("Enabled"))
        .RaceDisable(false)
        .Options(
            CheckboxOptions().Tooltip(
                "Shows an on-screen virtual controller for touch screens.\\nOn "
                "desktop the mouse can drive it for testing."
            )
        );
"""

NEW_FALLBACK = """#ifdef __IOS__
    // PAPERBOAT_IOS (overlay 0021 rev 3, D27): on iOS this checkbox is the
    // FALLBACK pad, so it says so. The shipped pad is the program's UIKit layer
    // (overlay 0024); this one is upstream's engine-drawn pad, and the two are
    // MUTUALLY EXCLUSIVE by construction - overlay 0028 makes
    // PaperboatGui::Enabled() false whenever gSohIos.TouchLayer is set, and the
    // shell's layer stands down whenever it is clear. The callback is what
    // moves that single switch, so there is no state in which both pads draw.
    //
    // The user's 0.0.0.8 report is what this exists for: his config carries
    // gTouchControls.Enabled = 1 from 0.0.0.5, and before 0028 that saved value
    // beat overlay 0024's default and brought upstream's pad back a second
    // after launch.
    AddWidget(path, "Use Upstream Touch Pad", WIDGET_CVAR_CHECKBOX)
        .CVar(CVAR_TOUCH("Enabled"))
        .RaceDisable(false)
        .Callback([](WidgetInfo& info) {
            CVarSetInteger("gSohIos.TouchLayer", CVarGetInteger(CVAR_TOUCH("Enabled"), 0) != 0 ? 0 : 1);
        })
        .Options(
            CheckboxOptions().DefaultValue(false).Tooltip(
                "Use this port's own engine-drawn touch pad instead of the iOS "
                "touch layer.\\n\\nOnly one pad is ever on screen: turning this on "
                "switches the iOS layer off, and turning it off brings the iOS "
                "layer (and Settings > iOS) back."
            )
        );
#else
    AddWidget(path, "Enable Touch Controls", WIDGET_CVAR_CHECKBOX)
        .CVar(CVAR_TOUCH("Enabled"))
        .RaceDisable(false)
        .Options(
            CheckboxOptions().Tooltip(
                "Shows an on-screen virtual controller for touch screens.\\nOn "
                "desktop the mouse can drive it for testing."
            )
        );
#endif
"""

OLD_SECTION = """    // Settings > Audio
    path.sidebarName = "Audio";
"""

NEW_SECTION = """#ifdef __IOS__
    // -----------------------------------------------------------------------
    // PAPERBOAT_IOS (overlay 0021): Settings > iOS.
    //
    // Every gSohIos.* CVar below is consumed by app/ios/PaperBoatIosShell.m or
    // by overlays 0019/0022/0023, and the defaults here mirror those exactly so
    // the menu can never disagree with the code that reads them. ASCII only -
    // the menu's font atlas has no em-dash or bullet glyph.
    // -----------------------------------------------------------------------
    path.sidebarName = "iOS";
    path.column = SECTION_COLUMN_1;
    AddSidebarEntry("Settings", "iOS", 1);

    AddWidget(path, "Touch Controls", WIDGET_SEPARATOR_TEXT);
    AddWidget(path, "Customize Layout...", WIDGET_BUTTON)
        .RaceDisable(false)
        .Options(
            ButtonOptions().Tooltip(
                "Drag any button (and the stick's home position) to move it, "
                "scale the whole layout, or hide buttons you don't use. Save or "
                "reset from the on-screen chrome."
            )
        )
        .Callback([](WidgetInfo& info) {
            // The SHELL's customizer (overlay 0024, D25), NOT upstream's. The
            // pad on screen is the program's UIKit layer; its editor is the one
            // that can move its chips, scale them and hide them. This CVar is a
            // transient trigger the overlay consumes on its next sync tick and
            // immediately clears, so a config flush mid-edit can never relaunch
            // the app straight into the customizer.
            CVarSetInteger("gSohIos.EditLayout", 1);
            Ship::Context::GetRawInstance()->GetWindow()->GetGui()->GetMenu()->Hide();
        });
    // gSohIos.TouchOpacity, not upstream's gTouchControls.Opacity: the pad this
    // slider dims is the SHELL's UIKit layer (overlay 0024), which cannot read
    // upstream's CVar without the shell becoming an engine client. Upstream's
    // own slider stays in Settings > Controls for upstream's own pad.
    AddWidget(path, "Touch Control Opacity", WIDGET_CVAR_SLIDER_FLOAT)
        .CVar("gSohIos.TouchOpacity")
        .RaceDisable(false)
        .Options(
            FloatSliderOptions().Min(0.1f).Max(1.0f).DefaultValue(0.8f).IsPercentage().Tooltip(
                "Opacity of the on-screen controls."
            )
        );
    AddWidget(path, "Haptic Feedback", WIDGET_CVAR_COMBOBOX)
        .CVar("gSohIos.Haptics")
        .RaceDisable(false)
        .Options(
            ComboboxOptions().ComboMap(iosHapticsOptions).DefaultIndex(1).Tooltip(
                "Vibration when you press an on-screen button."
            )
        );
    AddWidget(path, "Stick Response", WIDGET_CVAR_COMBOBOX)
        .CVar("gSohIos.StickCurve")
        .RaceDisable(false)
        .Options(
            ComboboxOptions().ComboMap(iosStickCurveOptions).DefaultIndex(0).Tooltip(
                "Linear moves exactly as far as your thumb does. Precise needs a "
                "bigger movement for the same speed, which makes slow walking "
                "easier to hold."
            )
        );
    AddWidget(path, "Left-Handed Layout", WIDGET_CVAR_CHECKBOX)
        .CVar("gSohIos.LeftyFlip")
        .RaceDisable(false)
        .Options(
            CheckboxOptions().DefaultValue(false).Tooltip(
                "Mirrors the whole touch layout left to right, including any "
                "positions you customised."
            )
        );
    // NOT here: a per-button hide/show list. The shell's layout customizer has
    // one eye chip per button, which is the family's mechanism, and two ways to
    // hide the same button is a settings page that contradicts itself. The
    // engine-side list (overlay 0014) stays #ifndef __IOS__ in Controls.

    AddWidget(path, "Menu", WIDGET_SEPARATOR_TEXT);
    AddWidget(path, "Menu Scale", WIDGET_CVAR_SLIDER_FLOAT)
        .CVar("gSohIos.MenuScale")
        .RaceDisable(false)
        .Callback([](WidgetInfo& info) { GameEngine::Instance->ScaleImGui(); })
        .Options(
            FloatSliderOptions()
                .Min(0.6f)
                .Max(1.2f)
                .DefaultValue(0.85f)
                .ShowButtons(true)
                .IsPercentage()
                .Tooltip("Size of this menu and its text. Applies immediately.")
        );

    AddWidget(path, "Display", WIDGET_SEPARATOR_TEXT);
    // Labelled SSAA (the user, 0.0.0.8: "Supersampling should be called SSAA");
    // the tooltip still spells out what it does. DefaultValue is the SHELL's
    // device tier, not 1.00f: an unset CVar means "pick for me" and the engine
    // already renders at the tier, so a fixed 1.00f here made the widget
    // contradict the picture. This is the widget's default-value hook, not a
    // CVar seed - nothing is written to paperboat.cfg.json until the player
    // moves the slider, so a config still follows the phone it is opened on.
    // (Closes docs/parity-checklist.md's deferred item 1, D23's known gap.)
    AddWidget(path, "SSAA", WIDGET_CVAR_SLIDER_FLOAT)
        .CVar("gSohIos.Supersample")
        .RaceDisable(false)
        .Options(
            FloatSliderOptions().Min(1.00f).Max(2.00f).DefaultValue(PBIos_SsaaDefaultValue()).Format("%.2fx").Tooltip(
                "Renders above the screen's resolution and scales down, which "
                "removes shimmer on edges.\\n\\n1.00x already renders at your "
                "display's full native resolution. Higher values cost GPU time."
            )
        );
    AddWidget(path, "Max Frame Rate", WIDGET_CVAR_COMBOBOX)
        .CVar("gSohIos.MaxFps")
        .RaceDisable(false)
        .Options(
            ComboboxOptions()
                .ComboMap(iosMaxFpsOptions)
                .DefaultIndex(120) // defaultIndex is used as the default VALUE
                .Tooltip(
                    "Caps how fast the game may render. 120 needs a ProMotion "
                    "display; on a 60 Hz screen both settings behave the same."
                )
        );
    AddWidget(path, "Performance HUD", WIDGET_CVAR_CHECKBOX)
        .CVar("gSohIos.PerfHud")
        .RaceDisable(false)
        .Options(
            CheckboxOptions().DefaultValue(false).Tooltip(
                "Shows frame rate and thermal state in the corner of the screen."
            )
        );

    // Round 13 (the ~5 s stutter, the design notes D26). Both of these are here
    // rather than under Display because neither is a thing a player tunes for
    // taste - they exist so a stutter report can be A/B'd in one session.
    AddWidget(path, "Advanced", WIDGET_SEPARATOR_TEXT);
    AddWidget(path, "Async Shaders", WIDGET_CVAR_CHECKBOX)
        .CVar("gSohIos.AsyncShaders")
        .RaceDisable(false)
        .Options(
            CheckboxOptions().DefaultValue(true).Tooltip(
                "Compiles graphics shaders in the background instead of stopping "
                "the game for them.\\n\\nOn: new effects may pop in a frame or "
                "two late the first time you see them. Off: the game freezes for "
                "about a sixth of a second instead. Leave this on."
            )
        );
    AddWidget(path, "Audio Buffer", WIDGET_CVAR_SLIDER_INT)
        .CVar("gSohIos.AudioBufferMs")
        .RaceDisable(false)
        .Options(
            IntSliderOptions().Min(30).Max(90).DefaultValue(90).Format("%d ms").Tooltip(
                "How much sound is kept ready ahead of time. Lower is slightly "
                "more responsive; higher survives a slow moment without a click."
                "\\n\\nTakes effect after you restart the app."
            )
        );
#if PAPERBOAT_REMOTE_CONSOLE
    AddWidget(path, "Remote Console", WIDGET_CVAR_CHECKBOX)
        .CVar("gSohIos.RemoteConsole")
        .RaceDisable(false)
        .Options(
            CheckboxOptions().DefaultValue(false).Tooltip(
                "Developer feature: opens an unauthenticated command server on "
                "your local network. Leave off unless you were asked to turn it "
                "on. Takes effect within a couple of seconds; turning it back "
                "off needs a relaunch."
            )
        );
#endif
#endif // __IOS__

    // Settings > Audio
    path.sidebarName = "Audio";
"""
menu = replace_once(menu, OLD_SECTION, NEW_SECTION, "iOS section insertion point", MENU_REL)
menu = replace_once(menu, OLD_FALLBACK, NEW_FALLBACK, "upstream touch-controls checkbox", MENU_REL)

# ---------------------------------------------------------------------------
# 2. PaperboatGui.cpp - the performance HUD
# ---------------------------------------------------------------------------
gui_src = VENDOR / GUI_REL
gui_orig = gui_src.read_text()
gui = gui_orig

OLD_HUD_CLASS = """UIWidgets::Colors GetMenuThemeColor() {
    return mPaperboatMenu->GetMenuThemeColor();
}
"""

NEW_HUD_CLASS = """UIWidgets::Colors GetMenuThemeColor() {
    return mPaperboatMenu->GetMenuThemeColor();
}

#ifdef __IOS__
// ---------------------------------------------------------------------------
// PAPERBOAT_IOS (overlay 0021): the performance HUD, gSohIos.PerfHud.
//
// Registered always, draws only when the CVar is on. Overrides Draw() (not
// DrawElement) and paints into ImGui's foreground draw list, the same shape as
// upstream's own TouchControlsOverlay -- no ImGui window, no title bar, nothing
// for a finger to land on.
//
// The numbers come from the Phase 0.3 probe through the SAME entry point the
// console bridge's `fps` verb uses (PBIos_PerfReport, overlay 0012), so the HUD
// and the bridge cannot drift apart. The probe's rings and mutex stay private
// to Engine.cpp; the cost is parsing one known line, which is three strstr's.
// ---------------------------------------------------------------------------
extern "C" int PBIos_PerfReport(char* out, int cap);
extern "C" void PBIos_GetSafeAreaInsets(float* top, float* left, float* bottom, float* right);

namespace {

double PBIosPerfField(const char* line, const char* key) {
    const char* p = std::strstr(line, key);
    return p != nullptr ? std::atof(p + std::strlen(key)) : 0.0;
}

class PBIosPerfHudWindow final : public Ship::GuiWindow {
  public:
    using GuiWindow::GuiWindow;

    void Draw() override;
    void InitElement() override {};
    void DrawElement() override {};
    void UpdateElement() override {};
};

void PBIosPerfHudWindow::Draw() {
    if (!CVarGetInteger("gSohIos.PerfHud", 0)) {
        return;
    }
    char report[512];
    if (PBIos_PerfReport(report, (int)sizeof(report)) <= 0) {
        return;
    }

    char text[96];
    if (std::strstr(report, "warming up") != nullptr) {
        std::snprintf(text, sizeof(text), "perf: warming up");
    } else {
        const double fps = PBIosPerfField(report, "fps=");
        const int thermal = (int)PBIosPerfField(report, "thermal=");
        // ROUND 22: SoH's exact three cases (SohIosShell.m
        // SohIos_PerfHud3DText) — the whole-number frame rate alone, with no
        // "fps" word and no frame time, and the thermal state spelled out only
        // when it is not nominal, because a throttled run is not a comparable
        // run and that is the one thing a glance must not miss. ASCII hyphen:
        // this atlas has no bullet, and SoH's engine-side text uses the hyphen
        // form too. The user asked for the HUDs to match across the family.
        if (thermal >= 2) {
            std::snprintf(text, sizeof(text), "%.0f - HOT", fps);
        } else if (thermal == 1) {
            std::snprintf(text, sizeof(text), "%.0f - warm", fps);
        } else {
            std::snprintf(text, sizeof(text), "%.0f", fps);
        }
    }

    // Top-right, inside the safe rect. The insets arrive in DRAWABLE PIXELS,
    // which is the space ImGui runs in on iOS in this fork (D16) and therefore
    // the space DisplaySize is in.
    float top = 0.0f, left = 0.0f, bottom = 0.0f, right = 0.0f;
    PBIos_GetSafeAreaInsets(&top, &left, &bottom, &right);
    const ImVec2 display = ImGui::GetIO().DisplaySize;
    const ImVec2 size = ImGui::CalcTextSize(text);
    const float pad = ImGui::GetFontSize() * 0.35f;
    const ImVec2 pos(display.x - right - size.x - pad * 3.0f, top + pad * 2.0f);

    ImDrawList* drawList = ImGui::GetForegroundDrawList();
    drawList->AddRectFilled(ImVec2(pos.x - pad, pos.y - pad * 0.5f),
                            ImVec2(pos.x + size.x + pad, pos.y + size.y + pad * 0.5f), IM_COL32(0, 0, 0, 140),
                            pad * 0.6f);
    drawList->AddText(pos, IM_COL32(255, 255, 255, 220), text);
}

} // namespace
#endif // __IOS__
"""
gui = replace_once(gui, OLD_HUD_CLASS, NEW_HUD_CLASS, "HUD window class", GUI_REL)

OLD_REGISTER = """    mTouchControlsOverlay = std::make_shared<TouchControlsOverlay>(CVAR_WINDOW("TouchControls"), "##TouchControls");
    gui->AddGuiWindow(mTouchControlsOverlay);
    mTouchControlsOverlay->Show();
}
"""

NEW_REGISTER = """    mTouchControlsOverlay = std::make_shared<TouchControlsOverlay>(CVAR_WINDOW("TouchControls"), "##TouchControls");
    gui->AddGuiWindow(mTouchControlsOverlay);
    mTouchControlsOverlay->Show();

#ifdef __IOS__
    // PAPERBOAT_IOS (overlay 0021): added AFTER the touch overlay so it paints
    // over the pad, not under it. The Gui keeps the only strong reference;
    // Destroy()'s RemoveAllGuiWindows() takes it with everything else.
    auto iosPerfHud = std::make_shared<PBIosPerfHudWindow>(CVAR_WINDOW("IosPerfHud"), "##IosPerfHud");
    gui->AddGuiWindow(iosPerfHud);
    iosPerfHud->Show();
#endif
}
"""
gui = replace_once(gui, OLD_REGISTER, NEW_REGISTER, "gui element registration", GUI_REL)

OLD_INCLUDES = """#include <imgui.h>
#include <imgui_internal.h>
#include <spdlog/spdlog.h>
"""

NEW_INCLUDES = """#include <imgui.h>
#include <imgui_internal.h>
#include <spdlog/spdlog.h>

#ifdef __IOS__
// PAPERBOAT_IOS (overlay 0021): the perf HUD formats itself out of the probe's
// one-line report.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#endif
"""
gui = replace_once(gui, OLD_INCLUDES, NEW_INCLUDES, "includes", GUI_REL)

OUT.write_text(__doc__ + "\n" + unified(MENU_REL, menu_orig, menu) + unified(GUI_REL, gui_orig, gui))
print(f"wrote {OUT}")
