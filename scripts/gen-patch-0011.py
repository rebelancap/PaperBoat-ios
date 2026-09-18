#!/usr/bin/env python3
"""Overlay patch 0011 — thread-safe CVar map (LUS ConsoleVariable).

CLASS: (a) program-baseline parity.

This is the SoH port's overlay 0030, re-authored against PaperBoat's LUS fork
(`JeodC/libultraship` @ `lus-converge`). None of the SoH port's hunks apply:
the file layout differs (`libultraship/src/...` there,
`external/libultraship/src/...` here) and this fork's `ConsoleVariable.h` is a
documented, reordered rewrite. Only the *fix* is ported.

THE BUG. `ConsoleVariable::mVariables` is a bare `std::unordered_map` and
`docs/lus-divergence.md` counted **15 unguarded accesses** to it. PaperBoat is
a two-thread engine: `GameEngine::HandleAudioThread` runs the whole audio
update on its own thread (`src/port/Engine.cpp`), and the audio path reads
CVars (volume, enhancement toggles) while the MAIN thread writes them — every
menu interaction, every `set` over the console, every `Load()`. A write that
inserts a new key can rehash the map while the audio thread is inside
`find()`, which dereferences a freed bucket: SIGSEGV, on the audio thread,
inside `CVarGetInteger`. The SoH port measured this twice in one session on a
device before it fixed it; it looks like an engine bug and is not.

THE FIX. One `std::recursive_mutex` member, locked at every entry point that
touches `mVariables`:

  * `Get` — **every** value read funnels through it (`GetInteger`, `GetFloat`,
    `GetString`, `GetColor`, `GetColor24` all call it, as do the five
    `Register*`), so one lock there covers the entire read side.
  * the five `Set*` — the writers, and the ones that rehash.
  * `ClearVariable` (five `erase` calls plus one), `CopyVariable` (two
    `operator[]`), `Save` (iterates the map), `Load` (clears it).

`recursive_mutex` and not `mutex` because these re-enter each other:
`Register*` → `Get` + `Set*`, `ClearVariable` → `Get`, `Load` →
`LoadFromPath` → `Set*`, `ClearBlock` → `Load`. A plain mutex self-deadlocks
on the first `RegisterInteger` of the boot sequence.

`ClearBlock` and `LoadFromPath`/`LoadLegacy` are deliberately NOT locked
directly: neither touches `mVariables` itself, and both reach it only through
functions that do (`Load`, `Set*`). Locking them as well would be harmless but
would claim a guarantee about `conf` that this patch is not making.

COST. An uncontended `recursive_mutex` lock/unlock is tens of nanoseconds
against a few hundred CVar calls per frame — below the noise floor of the
frame-time probe in overlay 0012.

RESIDUAL, documented and not fixed (same as the SoH port's): `GetString`
returns the raw `char*` out of the map. A concurrent `SetString` for the same
name `free()`s it, so the pointer can dangle after the lock is dropped. No
audio-thread call site reads a string CVar today; fixing it properly means
changing the public signature to return a `std::string`, which is an upstream
API change, not an overlay's business.

CONSEQUENCE FOR THE CONSOLE BRIDGE (`app/ios/PaperBoatIosConsole.cpp`): the
bridge hops every command onto the main queue and polls for the answer, and
the *stated* reason was exactly this missing lock (`docs/console-bridge.md`,
"Threading — the rule that keeps this safe"). With this patch in, the
CVar-specific reason is gone — `set`/`get` from the socket thread would now be
safe. **The hop stays anyway** (the design notes D14): `Console::Run` dispatches to
arbitrary command handlers, and the ones the port registers (`bind`, and
anything upstream adds later) touch ImGui and the control deck, which are
main-thread-only for reasons this patch does not address. One rule — "commands
run on the game loop's thread" — is cheaper to keep true than a per-command
exception list.

UPSTREAMABLE: yes, as-is, modulo the comment prefix.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0011-lus-cvar-thread-safety.patch"

LUS = "external/libultraship"
EDITS = []

# ------------------------------------------------- include/ship/config/ConsoleVariable.h
H_INCLUDE_OLD = """#include <stdint.h>
#include <memory>
#include <unordered_map>
"""
H_INCLUDE_NEW = """#include <stdint.h>
#include <memory>
#include <mutex>
#include <unordered_map>
"""

H_MEMBER_OLD = """    std::unordered_map<std::string, std::shared_ptr<CVar>, TransparentStringHash, TransparentStringEqual> mVariables;
};
"""
H_MEMBER_NEW = """    std::unordered_map<std::string, std::shared_ptr<CVar>, TransparentStringHash, TransparentStringEqual> mVariables;
    /**
     * @brief Guards @ref mVariables against concurrent access (PAPERBOAT_IOS,
     * overlay 0011).
     *
     * The engine is multi-threaded: the audio thread reads CVars while the main
     * thread writes them (menus, the console, Load()). An insert that rehashes
     * the map while another thread is inside find() dereferences a freed bucket.
     *
     * Recursive because the accessors re-enter each other: Register*() calls
     * Get() then Set*(), ClearVariable() calls Get(), Load() reaches Set*()
     * through LoadFromPath().
     */
    mutable std::recursive_mutex mVariablesMutex;
};
"""

EDITS.append((f"{LUS}/include/ship/config/ConsoleVariable.h",
              [(H_INCLUDE_OLD, H_INCLUDE_NEW, 1), (H_MEMBER_OLD, H_MEMBER_NEW, 1)]))

# ------------------------------------------------------ src/ship/config/ConsoleVariable.cpp
GUARD = "    std::lock_guard<std::recursive_mutex> pbLock(mVariablesMutex);\n"

CPP_EDITS = []

CPP_EDITS.append((
    """std::shared_ptr<CVar> ConsoleVariable::Get(const char* name) {
    auto it = mVariables.find(name);
""",
    """std::shared_ptr<CVar> ConsoleVariable::Get(const char* name) {
    // PAPERBOAT_IOS (overlay 0011): every CVar READ in the engine funnels
    // through here (GetInteger/GetFloat/GetString/GetColor*/Register*), so this
    // one lock covers the whole read side. See the header.
""" + GUARD + """    auto it = mVariables.find(name);
""", 1))

# The five setters. Each begins with an identical two-line body, so they are
# disambiguated by their signature line and matched one at a time.
for sig in ("void ConsoleVariable::SetInteger(const char* name, int32_t value) {",
            "void ConsoleVariable::SetFloat(const char* name, float value) {",
            "void ConsoleVariable::SetString(const char* name, const char* value) {",
            "void ConsoleVariable::SetColor(const char* name, Color_RGBA8 value) {",
            "void ConsoleVariable::SetColor24(const char* name, Color_RGB8 value) {"):
    CPP_EDITS.append((sig + "\n", sig + "\n" + GUARD, 1))

CPP_EDITS.append((
    """void ConsoleVariable::ClearVariable(const char* name) {
    std::shared_ptr<Config> conf""",
    """void ConsoleVariable::ClearVariable(const char* name) {
""" + GUARD + """    std::shared_ptr<Config> conf""", 1))

CPP_EDITS.append((
    """void ConsoleVariable::CopyVariable(const char* from, const char* to) {
    auto& variableFrom""",
    """void ConsoleVariable::CopyVariable(const char* from, const char* to) {
""" + GUARD + """    auto& variableFrom""", 1))

CPP_EDITS.append((
    """void ConsoleVariable::Save() {
    std::shared_ptr<Config> conf""",
    """void ConsoleVariable::Save() {
    // PAPERBOAT_IOS (overlay 0011): iterates the whole map. Held for the whole
    // serialisation, which is also what makes the iOS resign-active flush
    // (app/ios/PaperBoatIosShell.m) safe against a mid-write insert.
""" + GUARD + """    std::shared_ptr<Config> conf""", 1))

CPP_EDITS.append((
    """void ConsoleVariable::Load() {
    std::shared_ptr<Config> conf""",
    """void ConsoleVariable::Load() {
    // PAPERBOAT_IOS (overlay 0011): clears and repopulates the map; recursive
    // because LoadFromPath() calls the Set*() family, which locks again.
""" + GUARD + """    std::shared_ptr<Config> conf""", 1))

EDITS.append((f"{LUS}/src/ship/config/ConsoleVariable.cpp", CPP_EDITS))

chunks = []
for rel, subs in EDITS:
    src = VENDOR / rel
    orig = src.read_text()
    text = orig
    for old, new, expect in subs:
        n = text.count(old)
        assert n == expect, f"{rel}: expected {expect} match(es) of {old!r:.70}, got {n}"
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
