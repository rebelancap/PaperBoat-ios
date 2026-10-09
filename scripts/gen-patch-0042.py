#!/usr/bin/env python3
"""Overlay patch 0042 - iOS ROM import through the system document picker.

CLASS: (a) program-baseline parity (every sibling onboards through a picker),
also (b) an upstream UX bug worth sending. The design notes D42.

THE BUG. Upstream's mobile branch of GameExtractor::SelectGameFromUI has no
picker: it loads Documents/baserom.us.z64 or fails silently. With no ROM there,
the first-run prompt "No O2R files found. Generate one now?" -> Yes logs
"Failed to find ROM at path" and shows the SAME prompt again, forever; the only
other button exits the app. Nothing tells the user where the ROM has to go, and
the README promised "pick the .z64 in Files". (Reproduced on the lane-3
simulator, artifacts/sim/rom-import-2026-10-09/.)

THE FIX.
1. GameExtractor.cpp: on __IOS__ the branch calls the shell's
   PBIos_PickRomFile (app/ios/PaperBoatIosShell.m), which presents a
   UIDocumentPickerViewController and calls back on the main thread with the
   picked file's path (or NULL on cancel). The result goes through
   LoadRomFromPath -> onComplete exactly as the desktop picker's does, so
   RunExtract's existing async contract (PS_WAIT until onComplete) is reused.
   A cancel returns to the prompt. Android keeps upstream's fixed path.
2. Engine.cpp: on __IOS__ the "No O2R Files" prompt says what Yes does.
   A ROM already sitting in Documents is still found first by upstream's
   PS_LOCAL scan ("ROMs found"), so the old drop-in route keeps working.

Generate against the vendor tree with 0001..0041 applied. Idempotent: an
already-applied 0042 is reversed in memory first. Every edit asserts its count.
"""
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0042-paperboat-ios-rom-document-picker.patch"

EXT = "src/port/extractor/GameExtractor.cpp"
ENG = "src/port/Engine.cpp"

EDITS = [
    (EXT, [
        ("""#elif defined(__IOS__) || defined(__ANDROID__)
    // Mobile has no file dialog: the ROM is put in place beforehand.
    finish(LoadRomFromPath(Ship::Context::GetPathRelativeToAppDirectory("baserom.us.z64")));
#else
""", """#elif defined(__IOS__)
    // PAPERBOAT_IOS (overlay 0042, D42): the system document picker, presented
    // by the shell. It answers on a later main-thread run-loop turn (RunExtract
    // keeps pumping), with the picked file's path or NULL on cancel.
    auto* pending = new std::function<void(const char*)>([this, finish](const char* path) {
        finish(path != nullptr && LoadRomFromPath(path));
    });
    PBIos_PickRomFile(
        [](const char* path, void* user) {
            auto* fn = static_cast<std::function<void(const char*)>*>(user);
            (*fn)(path);
            delete fn;
        },
        pending);
#elif defined(__ANDROID__)
    // Mobile has no file dialog: the ROM is put in place beforehand.
    finish(LoadRomFromPath(Ship::Context::GetPathRelativeToAppDirectory("baserom.us.z64")));
#else
""", 1),
        ("""#include "port/FilePicker.h"
""", """#include "port/FilePicker.h"

#ifdef __IOS__
// PAPERBOAT_IOS (overlay 0042): app/ios/PaperBoatIosShell.m.
extern "C" void PBIos_PickRomFile(void (*done)(const char* path, void* user), void* user);
#endif
""", 1),
    ]),
    (ENG, [
        ("""                        PaperboatGui::RegisterPopup(
                            "No O2R Files", "No O2R files found. Generate one now?", "Yes", "No",
""", """                        PaperboatGui::RegisterPopup(
#ifdef __IOS__
                            // PAPERBOAT_IOS (overlay 0042, D42): Yes opens the document picker.
                            "Paper Mario ROM Needed",
                            "PaperBoat needs your Paper Mario (USA) ROM (.z64).\\n\\n"
                            "Tap Yes and choose it in Files. The game files are\\n"
                            "generated on this device; it takes a few minutes.",
                            "Yes", "Quit",
#else
                            "No O2R Files", "No O2R files found. Generate one now?", "Yes", "No",
#endif
""", 1),
    ]),
]


def udiff(rel, a_text, b_text):
    with tempfile.TemporaryDirectory() as td:
        fa = pathlib.Path(td) / "a"
        fb = pathlib.Path(td) / "b"
        fa.write_text(a_text)
        fb.write_text(b_text)
        r = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}", str(fa), str(fb)],
                           capture_output=True)
    assert r.returncode == 1, f"{rel}: diff produced no change"
    return r.stdout.decode()


chunks = []
for rel, subs in EDITS:
    cur = (VENDOR / rel).read_text()
    # Idempotency: undo an applied 0042 in memory (new -> old, asserted).
    applied = [new in cur for _, new, _ in subs]
    assert all(applied) or not any(applied), f"{rel}: 0042 half-applied"
    orig = cur
    if all(applied):
        for old, new, expect in subs:
            assert orig.count(new) == expect, f"{rel}: applied text count"
            orig = orig.replace(new, old, 1)
    text = orig
    for old, new, expect in subs:
        n = text.count(old)
        assert n == expect, f"{rel}: expected {expect} match(es) of {old!r:.80}, got {n}"
        text = text.replace(old, new, 1)
    chunks.append(udiff(rel, orig, text))

OUT.write_text(__doc__ + "\n" + "".join(chunks))
print(f"wrote {OUT.relative_to(ROOT)}")
