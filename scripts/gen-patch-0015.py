#!/usr/bin/env python3
"""Overlay patch 0015 — ConsoleVariable::SetString frees a non-string union.

CLASS: (b) upstream bug fix worth sending. Nothing in it is iOS-specific.

FOUND BY CRASHING, ROUND 6
--------------------------
Over the console bridge, while putting a dragged touch-layout position back:

    > set gTouchControls.Layout.Start.X -1
    Paperboat(12545) malloc: *** error for object 0x3d98b3a6:
                              pointer being freed was not allocated

`0x3d98b3a6` is not a pointer. It is IEEE-754 for **0.074561** — the exact
normalized X the layout editor had just saved for the Start button.

THE BUG
-------
`CVar` is a tagged union (`ship/config/ConsoleVariable.h`): `Type` says which
of `Integer` / `Float` / `String` / `Color` / `Color24` is live, and only the
`String` arm is heap-allocated. `SetString` frees the old string **without
consulting `Type`**:

    variable->Type = ConsoleVariableType::String;
    if (variable->String != nullptr) {
        free(variable->String);     // <- the union may hold a float or an int
    }
    variable->String = strdup(value);

So setting any previously-numeric CVar to a string reinterprets its bits as a
`char*` and frees them. Any non-zero float or integer is a non-null "pointer",
which makes this a near-certain heap abort rather than a rare one.

WHY IT MATTERS HERE MORE THAN IT LOOKS
--------------------------------------
The engine's own callers are typed (`CVarSetFloat` on a float, and so on), so
the game does not hit it. **The console does.** libultraship's `set` command
dispatches on the *literal's* shape, not on the variable's existing type, so
`set <any float cvar> <value>` can route to `SetString` — and the console is
exactly how this port drives the app: `scripts/run-sim.sh` + `nc localhost
8771` is the whole remote debugging story (docs/console-bridge.md). A tool that
kills the process it is inspecting is worse than no tool, and the failure looks
like a crash in whatever the app was doing at the time.

THE FIX
-------
Remember the type the variable had before the assignment, and free only if it
really was a string. Three lines, no behaviour change for a genuine
string-to-string set. Converting away from a string (`SetFloat` on a CVar that
held one) still leaks that string — that is a second, smaller bug in every
other setter, left alone here so this patch stays one hunk and one claim.

HUNK PLACEMENT (spec trap): overlay 0011 inserts the CVar mutex at the head
of this same function. This hunk starts three lines below the last line 0011's
hunk carries as context, so the two never overlap and the series stays
idempotent (`apply-overlay.sh` probes "already applied" by reverse-applying at
fuzz=0). `scripts/overlay-drill.sh` is the check that this is actually true.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "external/libultraship/src/ship/config/ConsoleVariable.cpp"
OUT = ROOT / "overlay/patches/0015-lus-cvar-setstring-union-free.patch"

src = VENDOR / REL
orig = src.read_text()

OLD = """    variable->Type = ConsoleVariableType::String;
    if (variable->String != nullptr) {
        free(variable->String);
    }
    variable->String = strdup(value);
"""

NEW = """    // PAPERBOAT_IOS (overlay 0015): only the String arm of the union is heap
    // allocated. Freeing unconditionally reinterprets a previously-stored
    // float or int as a char* and hands it to free() — measured:
    // `set <a float cvar> <value>` from the console aborts the process with
    // "pointer being freed was not allocated 0x3d98b3a6", which is the float
    // 0.074561 the variable actually held. Capture the old type first.
    const ConsoleVariableType previousType = variable->Type;
    variable->Type = ConsoleVariableType::String;
    if (previousType == ConsoleVariableType::String && variable->String != nullptr) {
        free(variable->String);
    }
    variable->String = strdup(value);
"""

n = orig.count(OLD)
assert n == 1, f"{REL}: expected 1 match of SetString's free block, got {n}"
text = orig.replace(OLD, NEW)

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
