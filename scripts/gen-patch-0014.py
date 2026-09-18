#!/usr/bin/env python3
"""Overlay patch 0014 — the program's unified touch layout + per-button hide/show.

CLASS: (a) program-baseline parity.

THE DECISION THIS IMPLEMENTS
----------------------------
The spec "What makes this port different", consequence 2: upstream's
engine-drawn pad versus the program's unified default layout, which the user
mandates identical across every HarbourMasters port. The default taken (nobody
vetoed it, and STATUS has carried it as the standing default since round 4):
**keep upstream's pad, re-tune its DEFAULT positions to the unified table, and
add the per-button hide/show it lacks.** The side-by-side the spec asks for
is `docs/touch-layout.md`; the decision is the design notes D17, the question is
QUESTIONS Q-004.

The unified table lives in `~/dev/harbourmasters/Ghostship-ios/docs/PORTING-DELTAS.md`
("SIBLING-WIDE: unified touch button-position defaults", device-tuned by the
user on Ghostship) and in that port's `docs/touch-layout.md`.

THE COORDINATE CONVERSION, AND WHY IT IS NOT A STRAIGHT COPY
------------------------------------------------------------
The unified table is in **UIKit points, edge-relative** (`maxX-85`, …), because
every sibling's pad is a UIKit overlay drawn by the shell. Upstream's pad is
drawn by the engine in **drawable pixels**, and sizes everything in a layout
unit `u = min(w, h) * 0.055 * scale` so the pad scales with the screen.

Converting through the reference device this port is developed on — iPhone Air,
912x420 pt landscape, nativeScale 3, so `u = 1260 px * 0.055 = 69.3 px =
23.1 pt` — gives the numbers below. On that device the defaults land on the
unified table almost exactly; on a bigger screen they scale as upstream's pad
always has, which is the behaviour we are keeping.

    unified pt   ->  u        element
    85, 105          3.68, 4.55   A
    177, 123         7.66, 5.32   B
    153, 42          6.62, 1.82   Z
    105, 273/197     4.55, 11.82/8.53  C-up / C-down
    145/65, 235      6.28/2.81, 10.17  C-left / C-right
    80, 60           3.46, 2.60   L (and R, mirrored)
    midX, 45         —,    1.95   Start
    midX, 55         —,    2.38   menu

Three places where a straight copy would be wrong, and what is done instead:

* **L and R are BARS here, not 34 pt discs.** Upstream's L/R/Z sprites are wide
  shoulder bars (aspect ~6.6, so `halfW` is 3.67u at the shipped `halfH`).
  Pinning a bar's CENTRE to the disc's centre would hang L off the left edge
  entirely. What transfers is the element's OUTER EDGE inset — the unified L
  disc's left edge sits 2.0u in, so the bar's left edge does too, and its
  centre follows from its own half-width. The vertical position is unified's,
  unchanged.
* **Z is a bar too**, but it is centred on the unified centre rather than
  edge-matched: the intent recorded with the table is "below A/B, equidistant
  from both, thumb-reachable", and for a wide bar spanning under both, the
  centre is the faithful reading. Checked: its top edge clears A's bottom by
  0.68u and its bottom edge sits 1.27u above the safe edge.
* **The D-pad is not in the unified table at all** (no sibling's pad has one).
  PM64's is upstream's own, and `Controls.DPadAsLeftStick` makes it a real
  movement control, so it stays exactly where upstream put it — and is now
  hideable for players who do not want it.

Note also that the menu button moves from the top-LEFT corner to top-centre
(unified), which is what frees the top-left for L. The edit-mode Done/Reset
pills move out to ±4.2u so their inner edges clear the menu button's radius,
and the edit-mode hint text drops to 4.4u so it clears it too.

ONLY DEFAULTS MOVE
------------------
`LoadPos` returns a saved `gTouchControls.Layout.<id>.{X,Y}` unchanged and only
falls through to these numbers when there is none, so **a layout the player
already made is untouched** — this patch cannot clobber one. Nothing bakes the
defaults into the config at first run either: upstream registers exactly three
touch CVars (`Enabled`, `Scale`, `Opacity`) and writes a position only when the
editor saves a drag. Verified by reading the saved config from a simulator
container that has run every round so far: no `Layout.*` keys in it.

Element SIZES are also left alone. The unified table's radii are for circles;
these are cropped sprites with fixed aspect ratios, and re-sizing them is a
separate argument from where they sit. `docs/touch-layout.md` records the
resulting divergence per element.

PER-BUTTON HIDE/SHOW
--------------------
Upstream's layout editor can MOVE a widget but not remove one (read end to end:
drag, Done, Reset, and nothing else). So: one persisted CVar per element,
`gTouchControls.Show.<id>`, defaulting to 1, applied as a single filter at the
end of `ComputeLayout` — which takes a hidden button out of the hit test and
the draw at once, with no second definition of "present" to keep in sync — and
surfaced as a checkbox list in the settings menu beside the existing touch
controls, where `Enabled`/`Scale`/`Opacity` already live.

**The stick and the menu button are deliberately not hideable.** Hiding the
stick removes all movement; hiding the menu button locks the player out of the
menu that would bring it back, and there is no keyboard on a phone. Every other
element is fair game.

The editor's Reset now clears visibility as well as position, and iterates the
element table rather than the live button list — the live list no longer
contains the hidden buttons, so a reset driven from it could never bring one
back (the bug this ordering avoids: hide a button, press Reset, and it stays
hidden forever with no UI left to find it).
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0014-paperboat-touch-unified-layout.patch"

EDITS = {}

# ---------------------------------------------------------------------------
# src/port/ui/TouchControls.h — the settings menu's view of the flags
# ---------------------------------------------------------------------------
H = "src/port/ui/TouchControls.h"
EDITS[H] = [
    ("""#ifdef __cplusplus
extern "C" void TouchControls_ApplyPad(void* pads);
#else
void TouchControls_ApplyPad(void* pads);
#endif
""",
     """#ifdef __cplusplus
extern "C" void TouchControls_ApplyPad(void* pads);
#else
void TouchControls_ApplyPad(void* pads);
#endif

// Per-element visibility, for the settings menu (overlay 0014). Index runs
// 0 .. TouchControls_HideableCount()-1. The returned strings are static and
// outlive the call, which the menu requires: WidgetInfo stores a widget's CVar
// name as a bare const char*, not a std::string.
#ifdef __cplusplus
extern "C" {
#endif
int TouchControls_HideableCount(void);
const char* TouchControls_HideableLabel(int index);
const char* TouchControls_HideableCVar(int index);
#ifdef __cplusplus
}
#endif
"""),
]

# ---------------------------------------------------------------------------
# src/port/ui/TouchControls.cpp
# ---------------------------------------------------------------------------
CPP = "src/port/ui/TouchControls.cpp"
EDITS[CPP] = []

# 1. <string>, for the static CVar-name table.
EDITS[CPP].append((
    """#include <algorithm>
#include <cmath>
#include <cstdio>
#include <vector>
""",
    """#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>
"""))

# 2. The visibility flags, just above RegisterCVars.
EDITS[CPP].append((
    """void RegisterCVars() {
    static bool registered = false;
""",
    """// --- per-element visibility (overlay 0014) ---------------------------------
// Upstream's layout editor can move a widget but not remove one, and a pad
// carrying buttons this game never asks for is a pad with less room for the
// ones it does. One persisted flag per element, defaulting to visible, keyed
// like a position so it travels with the rest of the layout.
//
// The analog stick and the menu button are NOT in this table on purpose:
// hiding the stick removes all movement, and hiding the menu button locks the
// player out of the settings page that would bring it back — there is no
// keyboard on a phone.
struct HideableElement {
    const char* id;
    const char* label;
};

const HideableElement kHideable[] = {
    { "A", "A" },
    { "B", "B" },
    { "CUp", "C-Up" },
    { "CDown", "C-Down" },
    { "CLeft", "C-Left" },
    { "CRight", "C-Right" },
    { "L", "L" },
    { "R", "R" },
    { "Z", "Z" },
    { "DUp", "D-Pad Up" },
    { "DDown", "D-Pad Down" },
    { "DLeft", "D-Pad Left" },
    { "DRight", "D-Pad Right" },
    { "Start", "Start" },
};
constexpr int kHideableCount = (int) (sizeof(kHideable) / sizeof(kHideable[0]));

void ShowKey(char* out, size_t outSize, const char* id) {
    snprintf(out, outSize, CVAR_TOUCH("Show.%s"), id);
}

bool ElementVisible(const char* id) {
    char key[64];
    ShowKey(key, sizeof(key), id);
    return CVarGetInteger(key, 1) != 0;
}

void ClearShow(const char* id) {
    char key[64];
    ShowKey(key, sizeof(key), id);
    CVarClear(key);
}

void RegisterCVars() {
    static bool registered = false;
"""))

# 3. The defaults themselves, plus the visibility filter.
EDITS[CPP].append((
    """    state.menuCenter = LoadPos("Menu", ImVec2(2.0f * u, 1.8f * u), w, h);
    state.menuRadius = 1.0f * u;

    state.editMode = CVarGetInteger(CVAR_TOUCH("EditMode"), 0) != 0;
    state.doneCenter = ImVec2(w * 0.5f + 3.4f * u, 1.6f * u);
    state.doneHalf = ImVec2(2.6f * u, 1.0f * u);
    state.resetCenter = ImVec2(w * 0.5f - 3.4f * u, 1.6f * u);
    state.resetHalf = ImVec2(2.6f * u, 1.0f * u);

    state.gameButtons.clear();
    // Face buttons, bottom-right.
    state.gameButtons.push_back(make(
        BTN_A, "A", "A", "A-Btn", "textures/buttons/ABtn.png", "A-Btn Outline", "textures/buttons/ABtnOutline.png", rA,
        kBlue, 0, 0, ImVec2(w - 2.6f * u, h - 3.0f * u), 1.5f * u
    ));
    state.gameButtons.push_back(make(
        BTN_B, "B", "B", "B-Btn", "textures/buttons/BBtn.png", "B-Btn Outline", "textures/buttons/BBtnOutline.png", rB,
        kGreen, 0, 0, ImVec2(w - 5.6f * u, h - 4.2f * u), 1.25f * u
    ));
    // C buttons in a diamond, above the face buttons.
    const ImVec2 c(w - 3.3f * u, h - 8.8f * u);
    const float cOff = 1.5f * u;
    const float cR = 0.85f * u;
    state.gameButtons.push_back(make(
        BTN_CUP, "CUp", "C", "C-Up", "textures/buttons/CUp.png", "C-Up Outline", "textures/buttons/CUpOutline.png",
        rCUp, kYellow, 0, -1, ImVec2(c.x, c.y - cOff), cR
    ));
    state.gameButtons.push_back(make(
        BTN_CDOWN, "CDown", "C", "C-Down", "textures/buttons/CDown.png", "C-Down Outline",
        "textures/buttons/CDownOutline.png", rCDown, kYellow, 0, 1, ImVec2(c.x, c.y + cOff), cR
    ));
    state.gameButtons.push_back(make(
        BTN_CLEFT, "CLeft", "C", "C-Left", "textures/buttons/CLeft.png", "C-Left Outline",
        "textures/buttons/CLeftOutline.png", rCLeft, kYellow, -1, 0, ImVec2(c.x - cOff, c.y), cR
    ));
    state.gameButtons.push_back(make(
        BTN_CRIGHT, "CRight", "C", "C-Right", "textures/buttons/CRight.png", "C-Right Outline",
        "textures/buttons/CRightOutline.png", rCRight, kYellow, 1, 0, ImVec2(c.x + cOff, c.y), cR
    ));
    // Shoulder / trigger bars: L top-left (clear of the menu button), R and Z top-right.
    state.gameButtons.push_back(make(
        BTN_L, "L", "L", "L-Btn", "textures/buttons/LBtn.png", "L-Btn Outline", "textures/buttons/LBtnOutline.png", rL,
        kGray, 0, 0, ImVec2(7.5f * u, 1.6f * u), 0.55f * u
    ));
    state.gameButtons.push_back(make(
        BTN_R, "R", "R", "R-Btn", "textures/buttons/RBtn.png", "R-Btn Outline", "textures/buttons/RBtnOutline.png", rR,
        kGray, 0, 0, ImVec2(w - 4.2f * u, 1.6f * u), 0.55f * u
    ));
    state.gameButtons.push_back(make(
        BTN_Z, "Z", "Z", "Z-Btn", "textures/buttons/ZBtn.png", "Z-Btn Outline", "textures/buttons/ZBtnOutline.png", rZ,
        kGray, 0, 0, ImVec2(w - 4.2f * u, 3.4f * u), 0.55f * u
    ));
""",
    """    // The unified layout (overlay 0014) puts the menu button at top-centre,
    // which is what frees the top-left corner for L.
    state.menuCenter = LoadPos("Menu", ImVec2(w * 0.5f, 2.38f * u), w, h);
    state.menuRadius = 1.0f * u;

    state.editMode = CVarGetInteger(CVAR_TOUCH("EditMode"), 0) != 0;
    // The pills flank the menu button: +-4.2u leaves their inner edges 1.6u
    // out, clear of its 1.0u radius.
    state.doneCenter = ImVec2(w * 0.5f + 4.2f * u, 1.6f * u);
    state.doneHalf = ImVec2(2.6f * u, 1.0f * u);
    state.resetCenter = ImVec2(w * 0.5f - 4.2f * u, 1.6f * u);
    state.resetHalf = ImVec2(2.6f * u, 1.0f * u);

    // Default positions below are the PROGRAM-WIDE unified layout, converted
    // from its UIKit points through this port's layout unit (u = 23.1 pt on
    // the iPhone Air reference: 1260 px * 0.055 / scale 3). See
    // docs/touch-layout.md for the table, the conversion and the divergences.
    state.gameButtons.clear();
    // Face buttons, bottom-right. Unified: A (maxX-85, maxY-105) = (3.68u,
    // 4.55u) in from the corner; B is A + (-92, -18) pt.
    state.gameButtons.push_back(make(
        BTN_A, "A", "A", "A-Btn", "textures/buttons/ABtn.png", "A-Btn Outline", "textures/buttons/ABtnOutline.png", rA,
        kBlue, 0, 0, ImVec2(w - 3.68f * u, h - 4.55f * u), 1.5f * u
    ));
    state.gameButtons.push_back(make(
        BTN_B, "B", "B", "B-Btn", "textures/buttons/BBtn.png", "B-Btn Outline", "textures/buttons/BBtnOutline.png", rB,
        kGreen, 0, 0, ImVec2(w - 7.66f * u, h - 5.32f * u), 1.25f * u
    ));
    // C buttons, above the face buttons. The unified diamond is slightly taller
    // than it is wide (+-40 pt horizontally, +-38 pt vertically), so the two
    // offsets are separate rather than upstream's single cOff.
    const ImVec2 c(w - 4.55f * u, h - 10.17f * u);
    const float cOffX = 1.73f * u;
    const float cOffY = 1.65f * u;
    const float cR = 0.85f * u;
    state.gameButtons.push_back(make(
        BTN_CUP, "CUp", "C", "C-Up", "textures/buttons/CUp.png", "C-Up Outline", "textures/buttons/CUpOutline.png",
        rCUp, kYellow, 0, -1, ImVec2(c.x, c.y - cOffY), cR
    ));
    state.gameButtons.push_back(make(
        BTN_CDOWN, "CDown", "C", "C-Down", "textures/buttons/CDown.png", "C-Down Outline",
        "textures/buttons/CDownOutline.png", rCDown, kYellow, 0, 1, ImVec2(c.x, c.y + cOffY), cR
    ));
    state.gameButtons.push_back(make(
        BTN_CLEFT, "CLeft", "C", "C-Left", "textures/buttons/CLeft.png", "C-Left Outline",
        "textures/buttons/CLeftOutline.png", rCLeft, kYellow, -1, 0, ImVec2(c.x - cOffX, c.y), cR
    ));
    state.gameButtons.push_back(make(
        BTN_CRIGHT, "CRight", "C", "C-Right", "textures/buttons/CRight.png", "C-Right Outline",
        "textures/buttons/CRightOutline.png", rCRight, kYellow, 1, 0, ImVec2(c.x + cOffX, c.y), cR
    ));
    // Shoulder / trigger bars. The unified table's L/R are 34 pt DISCS 80 pt in
    // from the corner; these are wide bars, so what transfers is the outer
    // edge (2.0u in) and the vertical position (2.6u) — the centre follows
    // from the bar's own half-width, or L would hang off the screen entirely.
    // Z instead takes the unified CENTRE (maxX-153, maxY-42): the intent
    // recorded with the table is "below A/B, equidistant from both", and a bar
    // spanning under both is the faithful reading of that for this art.
    state.gameButtons.push_back(make(
        BTN_L, "L", "L", "L-Btn", "textures/buttons/LBtn.png", "L-Btn Outline", "textures/buttons/LBtnOutline.png", rL,
        kGray, 0, 0, ImVec2(2.0f * u + 0.55f * u * Aspect(rL), 2.6f * u), 0.55f * u
    ));
    state.gameButtons.push_back(make(
        BTN_R, "R", "R", "R-Btn", "textures/buttons/RBtn.png", "R-Btn Outline", "textures/buttons/RBtnOutline.png", rR,
        kGray, 0, 0, ImVec2(w - 2.0f * u - 0.55f * u * Aspect(rR), 2.6f * u), 0.55f * u
    ));
    state.gameButtons.push_back(make(
        BTN_Z, "Z", "Z", "Z-Btn", "textures/buttons/ZBtn.png", "Z-Btn Outline", "textures/buttons/ZBtnOutline.png", rZ,
        kGray, 0, 0, ImVec2(w - 6.62f * u, h - 1.82f * u), 0.55f * u
    ));
"""))

EDITS[CPP].append((
    """    // Start, bottom-center.
    state.gameButtons.push_back(make(
        BTN_START, "Start", "S", "Start-Btn", "textures/buttons/StartBtn.png", "Start-Btn Outline",
        "textures/buttons/StartBtnOutline.png", rStart, kRed, 0, 0, ImVec2(w * 0.5f, h - 1.7f * u), 0.9f * u
    ));
}
""",
    """    // Start, bottom-center (unified: midX, maxY-45 pt).
    state.gameButtons.push_back(make(
        BTN_START, "Start", "S", "Start-Btn", "textures/buttons/StartBtn.png", "Start-Btn Outline",
        "textures/buttons/StartBtnOutline.png", rStart, kRed, 0, 0, ImVec2(w * 0.5f, h - 1.95f * u), 0.9f * u
    ));

    // Per-element visibility (overlay 0014), applied as one filter rather than
    // a guard on each push_back: the table above stays readable, and a hidden
    // button leaves the hit test and the draw together, with no second
    // definition of "present" to keep in sync.
    state.gameButtons.erase(
        std::remove_if(
            state.gameButtons.begin(), state.gameButtons.end(),
            [](const TouchButton& button) { return !ElementVisible(button.id); }
        ),
        state.gameButtons.end()
    );
}
"""))

# 4. Reset clears visibility too, and iterates the TABLE, not the live list.
EDITS[CPP].append((
    """        for (const auto& button : state.gameButtons) {
            ClearPos(button.id);
        }
        ClearPos("Stick");
        ClearPos("Menu");
""",
    """        // Iterate the element TABLE, not state.gameButtons: the live list no
        // longer holds the hidden buttons, so a reset driven from it could
        // never bring one back — hide a button, press Reset, and it would stay
        // hidden with no widget left to find.
        for (const auto& element : kHideable) {
            ClearPos(element.id);
            ClearShow(element.id);
        }
        ClearPos("Stick");
        ClearPos("Menu");
"""))

# 5. The edit-mode hint drops clear of the now-central menu button.
EDITS[CPP].append((
    """            ImVec2(display.x * 0.5f, 3.6f * u), u * 0.9f, "Drag controls to reposition them",
""",
    """            ImVec2(display.x * 0.5f, 4.4f * u), u * 0.9f, "Drag controls to reposition them",
"""))

# 6. The exported accessors, outside the anonymous namespace.
EDITS[CPP].append((
    """} // namespace

extern "C" void TouchControls_ApplyPad(void* pads) {
""",
    """} // namespace

// The settings menu's view of the visibility flags (overlay 0014). The CVar
// name has to outlive the call — WidgetInfo keeps a bare const char* — so the
// keys are built once into a static table.
extern "C" int TouchControls_HideableCount(void) {
    return kHideableCount;
}

extern "C" const char* TouchControls_HideableLabel(int index) {
    return (index >= 0 && index < kHideableCount) ? kHideable[index].label : "";
}

extern "C" const char* TouchControls_HideableCVar(int index) {
    static std::string keys[kHideableCount];
    static bool built = false;
    if (!built) {
        built = true;
        for (int i = 0; i < kHideableCount; i++) {
            char key[64];
            ShowKey(key, sizeof(key), kHideable[i].id);
            keys[i] = key;
        }
    }
    return (index >= 0 && index < kHideableCount) ? keys[index].c_str() : "";
}

extern "C" void TouchControls_ApplyPad(void* pads) {
"""))

# ---------------------------------------------------------------------------
# src/port/ui/PaperboatMenuSettings.cpp — the checkbox list
# ---------------------------------------------------------------------------
MENU = "src/port/ui/PaperboatMenuSettings.cpp"
EDITS[MENU] = [
    ("""        .Callback([](WidgetInfo& info) {
            CVarSetInteger(CVAR_TOUCH("Enabled"), 1);
            CVarSetInteger(CVAR_TOUCH("EditMode"), 1);
            Ship::Context::GetRawInstance()->GetWindow()->GetGui()->GetMenu()->Hide();
        });

    path.column = SECTION_COLUMN_2;
""",
     """        .Callback([](WidgetInfo& info) {
            CVarSetInteger(CVAR_TOUCH("Enabled"), 1);
            CVarSetInteger(CVAR_TOUCH("EditMode"), 1);
            Ship::Context::GetRawInstance()->GetWindow()->GetGui()->GetMenu()->Hide();
        });
#ifndef __IOS__
    // Per-button hide/show (overlay 0014). The layout editor moves buttons but
    // cannot remove one. The analog stick and the menu button are absent from
    // this list on purpose: hiding either is a lockout, not a preference.
    //
    // NOT on iOS: overlay 0021 gives the phone a single Settings > iOS page and
    // this list lives there instead. One list, one place, per platform.
    AddWidget(path, "Visible Buttons", WIDGET_SEPARATOR_TEXT);
    for (int i = 0; i < TouchControls_HideableCount(); i++) {
        AddWidget(path, TouchControls_HideableLabel(i), WIDGET_CVAR_CHECKBOX)
            .CVar(TouchControls_HideableCVar(i))
            .RaceDisable(false)
            .Options(CheckboxOptions().DefaultValue(true).Tooltip("Draws this button on the touch pad."));
    }
#endif

    path.column = SECTION_COLUMN_2;
"""),
]

# ---------------------------------------------------------------------------

chunks = []
for rel, edits in EDITS.items():
    src = VENDOR / rel
    orig = src.read_text()
    text = orig
    for i, (old, new) in enumerate(edits):
        n = text.count(old)
        assert n == 1, f"{rel}: expected 1 match of edit {i}, got {n}"
        text = text.replace(old, new)
    with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
         tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
        fa.write(orig); fb.write(text); fa.flush(); fb.flush()
        r = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}",
                            fa.name, fb.name], capture_output=True)
    assert r.returncode == 1, f"{rel}: diff produced no change"
    chunks.append(r.stdout.decode())

OUT.write_text(__doc__ + "\n" + "".join(chunks))
print(f"wrote {OUT}")
