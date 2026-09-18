#!/usr/bin/env python3
"""Overlay patch 0003 — terminate D_8014EA48, the unterminated stencil display list.

CLASS: (b) upstream bug fix worth sending.

`src/screen_overlays.c`'s `Gfx D_8014EA48[]` ends with

    gsSP2Triangles(20, 22, 21, 0, 23, 21, 22, 0),
    gsDPPipeSync(),
    gsDPSetDepthSource(G_ZS_PIXEL)
};

with NO `gsSPEndDisplayList()`. It is entered with a PUSHING
`gSPDisplayList(gMainGfxPos++, D_8014EA48)` (line 317), so both consumers walk
off the end of the array into whatever static data the linker put next:

1. `port_patch_dl(D_8014EA48)` — `gbi_resolve_vtx_in_static_dl()`
   (src/port/GBIMiddleware.cpp) loops `for (Gfx* cmd = dl;; cmd++)` and breaks
   ONLY on `G_ENDDL`. Past the end it eventually reads a word whose top byte
   looks like `G_VTX`/`G_DL`, takes `words.w1` as a pointer and hands it to
   `GameEngine_OTRSigCheck()`, whose `< 0x10000` guard does not catch a large
   garbage value. `strncmp` then faults.
2. the Fast3D interpreter at render time, for the same reason.

On the iPhone Air simulator (iOS 27) this kills the app on its FIRST frame —
`_render_transition_stencil()` runs the one-shot `port_patch_dl` block before
the `progress == 0.0f` early-out, and `main_loop.c:289` calls it every boot:

    EXC_BAD_ACCESS (SIGSEGV) KERN_INVALID_ADDRESS at 0x000001ff01e00280
    0 libsystem_platform.dylib  _platform_strncmp
    1 Paperboat                 GameEngine_OTRSigCheck
    2 Paperboat                 gbi_resolve_vtx_in_static_dl
    3 Paperboat                 _render_transition_stencil
    4 Paperboat                 gfx_draw_frame

The faulting address is identical across runs — it is a constant read out of
static data, not a wild heap pointer, which is what pins this on data layout
rather than on chance. The macOS oracle survives the same code for exactly that
reason: a different link order happens to put a G_ENDDL-shaped word close
enough behind the array. It is undefined behaviour on both.

The fix is the missing terminator, and nothing else. There is no fall-through
to preserve: the next thing in the translation unit is a function, not another
Gfx array, so the overrun has never had a defined destination.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "src/screen_overlays.c"
OUT = ROOT / "overlay/patches/0003-paperboat-terminate-stencil-dl.patch"

src = VENDOR / REL
orig = src.read_text()

OLD = """    gsSP2Triangles(20, 22, 21, 0, 23, 21, 22, 0),
    gsDPPipeSync(),
    gsDPSetDepthSource(G_ZS_PIXEL)
};
"""
NEW = """    gsSP2Triangles(20, 22, 21, 0, 23, 21, 22, 0),
    gsDPPipeSync(),
    gsDPSetDepthSource(G_ZS_PIXEL),
    // PAPERBOAT_IOS: this terminator was missing. D_8014EA48 is entered with a
    // pushing gSPDisplayList (below), so without a G_ENDDL both the Fast3D
    // interpreter and port_patch_dl()'s gbi_resolve_vtx_in_static_dl() walk off
    // the end of the array into unrelated static data. On iOS that faults on
    // the first frame, inside GameEngine_OTRSigCheck, on a garbage pointer read
    // out of a word past the array.
    gsSPEndDisplayList()
};
"""

n = orig.count(OLD)
assert n == 1, f"{REL}: expected 1 match of the D_8014EA48 tail, got {n}"
text = orig.replace(OLD, NEW)

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
