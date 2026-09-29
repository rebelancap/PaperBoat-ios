#!/usr/bin/env python3
"""Overlay patch 0002 — Metal shader: drop the address-space qualifier from
texture2d/sampler parameters, and from the one by-value scalar that carries one.

AMENDED 2026-09-17 (round 10). The original patch stripped `thread const` from
the opaque texture/sampler parameters only, and the oracle still died exactly as
M-001 did. The surviving offender is `hookTexture2D`'s last parameter,
`thread const int filtering` — a BY-VALUE scalar, which Metal also refuses to
let carry an address space ("parameter may not be qualified with an address
space"). One token; proven against a scratch copy of the archive in round 9 and
folded into this patch here. See the design notes D4 addendum part 2 and MEASUREMENTS
"M-014 closed": absence of the "Failed to compile shader library" line is NOT
evidence Metal works — with the screen locked the backend never compiles a
library at all.

CLASS: (b) upstream bug fix worth sending. Upstream's Metal shader template
declares helper parameters as `thread const texture2d<float>` /
`thread const sampler`. Textures and samplers are opaque types in MSL and may
not carry an address-space qualifier at all; the macOS 27 / iOS 27 Metal
compiler enforces that and rejects the library:

    [gfx_metal.cpp:235] [error] Failed to compile shader library, error
    program_source:69:51: error: parameter may not be qualified with an address space
    float4 filter3point(thread const texture2d<float> tex, thread const sampler texSmplr, ...)
    program_source:77:52: error: parameter may not be qualified with an address space
    float4 hookTexture2D(thread const texture2d<float> tex, thread const sampler texSmplr, ...)
    program_source:77:143: error: parameter may not be qualified with an address space
    float4 hookTexture2D(..., thread const int filtering)

ShaderGetInfo() then dereferences the null program and the process dies with
EXC_BAD_ACCESS (macOS oracle: the measurement log M-001, crash report
Paperboat-2026-09-16-204805.ips). Metal is the only backend worth having on
iOS, so this is a hard prerequisite for the first rendered iOS frame.

`thread const float2&` and friends are LEFT ALONE: an address space on a
reference parameter is legal and meaningful. Only the opaque types and the one
by-value scalar change.

WHICH FILE SHIPS (revised for upstream 1.0.2, round 26). Upstream a90381914
"Use the shader files provided by LUS" deleted port/shaders/ and now stages
libultraship's src/fast/shaders/ into paperboat.o2r's shaders/ (root
CMakeLists.txt, GeneratePortO2R). So the file the app compiles is now
external/libultraship/src/fast/shaders/metal/default.shader.metal, which carries
the identical defect (same 6 + 6 + 1 occurrences) — the patch simply moved with
the file. srgb.shader.metal still has no such parameters.

Because the o2r is a CONFIGURE_DEPENDS glob over port/* and the LUS shaders, editing the shader
makes the build regenerate paperboat.o2r on its own.
"""
import subprocess, pathlib, tempfile, re

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "external/libultraship/src/fast/shaders/metal/default.shader.metal"
OUT = ROOT / "overlay/patches/0002-paperboat-metal-shader-address-space.patch"

src = VENDOR / REL
orig = src.read_text()

# Opaque types only. Asserted counts, so a silent no-op edit cannot ship.
EDITS = [
    ("thread const texture2d<float> ", "texture2d<float> ", 6),
    ("thread const sampler ", "sampler ", 6),
    # The amendment: the by-value scalar. Exactly one in the file.
    ("thread const int filtering", "const int filtering", 1),
]
text = orig
for old, new, want in EDITS:
    n = text.count(old)
    assert n == want, f"{REL}: expected {want} occurrences of {old!r}, got {n}"
    text = text.replace(old, new)

# Nothing legal should have been touched: references keep their address space.
assert text.count("thread const float2&") == orig.count("thread const float2&")
assert "thread const texture2d" not in text and "thread const sampler" not in text
# Nothing by-value may keep an address space: after the edits the only `thread`
# left in the file must be on reference parameters.
import re as _re
for _line in text.splitlines():
    for _m in _re.finditer(r"thread const [^,)&]*[,)]", _line):
        raise AssertionError(f"by-value parameter still address-qualified: {_line.strip()}")

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
