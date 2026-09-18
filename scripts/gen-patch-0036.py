#!/usr/bin/env python3
"""Overlay patch 0036 - CI textures decode to RGBA once at upload; the palette
lookup leaves the fragment shader.

CLASS: (b) upstream bug fix / perf fix worth sending to JeodC/libultraship. Not
iOS-gated: the macOS oracle is the ground truth and must render the same way, and
the A/B lever has to exist on the oracle too.

THE MEASUREMENT THAT MOTIVATED IT (M-030, the user's iPhone Air, A19-class, 120 Hz)
--------------------------------------------------------------------------------
    SSAA 2.0 (dims 5472x2520): gpu_game_ms p50=5.71 p95=12.24 max=34.2
    SSAA 1.0 (dims 2736x1260): gpu_game_ms p50=1.77 p95=3.44        (M-029)

2x is 4x the pixels, so a pixel-bound frame should land near 7 ms - and after
0033/0034 removed the empty render passes the p50 does (5.71). The p95 does not:
12.24 ms against an 8.33 ms slot, which is what costs presents. A cost that
scales with PIXELS and spikes on scene content is fragment work, and DECISIONS
D28 §2 named the suspect first: this fork does the CI4/CI8 palette lookup IN THE
FRAGMENT SHADER, unconditionally.

WHAT THE COST IS
----------------
`paletteSample` (port/shaders/metal/default.shader.metal, inside paperboat.o2r)
does not sample the texture. It samples the INDEX texture, scales the red channel
into a palette coordinate, and samples a second 256-entry palette texture with
that coordinate - a DEPENDENT texture read, where the address of the second tap
is a value the first tap returned. The GPU cannot prefetch it.

And it does that per FILTER TAP, because filtering must happen after the lookup
(indices do not interpolate):

    three-point (PM64's default):  3 index taps + 3 dependent palette taps
    bilinear:                      4 index taps + 4 dependent palette taps
    point:                         1 index tap  + 1 dependent palette tap

So the default path is 6 samples per texture unit, half of them dependent, where
a decoded RGBA texture is ONE hardware-filtered sample. In two-cycle mode with
both texels palettized it is 12. Paper Mario is almost entirely CI art, so this
is not an edge case - it is the common case, on nearly every fragment of nearly
every frame. That is exactly the shape of a cost that is invisible at 1x
(1.77 ms) and binding at 2x.

WHAT THE FIX IS - AND WHY IT IS ONE LINE
----------------------------------------
This fork ALREADY has the CPU decode. `ImportTextureCi4` / `ImportTextureCi8` /
`DecodeTileToRgba32` decode CI to RGBA32 with the TLUT on the CPU, and are used
today whenever a tile cannot be palettized (mip chains, masked or blended
textures, HD/raw replacements). GPU palettization is an OPTION layered on top of
that path, decided per draw in `Interpreter::Draw` and signalled to the shader
generator by SHADER_OPT(TEXEL0_PALETTE) / SHADER_OPT(TEXEL1_PALETTE).

So the change is to stop taking the option by default:

  * `mPaletteInShader`, latched once per frame in `Interpreter::Run` from the
    CVar `gSohIos.PaletteInShader` (default 0), next to the other per-frame
    latches (`mRgbDitherEnabled`, `mAsyncTextureLoad`) so the hot path never does
    a CVar lookup;
  * the `palettized[]` loop in `Draw` does not run when it is 0.

Everything else follows on its own, which is the point of doing it here and not
in the shader:

  * no TEXEL*_PALETTE bit is set, so `cc_features->used_palette[]` is false, so
    the shader template's `@if(o_palette[0] || o_palette[1])` blocks are NOT
    EMITTED - `paletteTap`, `paletteSample`, the palette texture parameter and
    its sampler are absent from the generated program entirely. The win is in the
    shader, and this is how it is taken out of the shader.
  * `AcquirePaletteTexture()` is never called, so the immutable palette-texture
    ring (up to 1024 x 256 x 4 B) is never populated and
    `SelectTexture(SHADER_PALETTE_TEXTURE, ...)` never runs.
  * `mImportIndexed` stays false, so `ImportTexture` builds the NON-indexed cache
    key - and that key was already correct for this: CI4 keys on
    `mRdp->palette_bank_dram_addr[paletteIndex & 15]`, CI8 on
    `mRdp->palette_dram_addr[0..1]`, both alongside `palette_index`, so the same
    raster drawn through a different TLUT is a DIFFERENT cache entry and decodes
    again. `TextureCacheDeleteByPalette` already drops entries when a TLUT's DRAM
    address is reloaded with new contents. PM64 tints and animates by palette
    swap, so this is the half that a single screenshot would not catch, and it
    needed no new code: the non-indexed key is the path the engine has always
    taken for masked/blended/mip CI tiles.

WHAT IT COSTS, HONESTLY
-----------------------
GPU MEMORY: nothing. The index texture is NOT uploaded as R8 - it is uploaded as
RGBA32 with the index in the red channel (`ImportTextureCi4`/`Ci8`, and the
comment above `UploadBaseTexture`'s mip guard says so). Both paths upload
width*height*4 bytes, so the decoded texture is exactly the same size as the
index texture it replaces. The "CI4 -> 8x, CI8 -> 4x" expansion is relative to
the raw N64 bits, which were never what got uploaded.

CACHE ENTRIES: this is the real cost. One index texture used to serve every TLUT
bound to it; now each (raster, palette) pair is its own entry in a cache capped
at TEXTURE_CACHE_MAX_SIZE = 1024 entries (LRU, count-bounded, not byte-bounded).
An N64 CI tile is at most 2 KB of TMEM, i.e. 4096 CI4 texels or 2048 CI8 texels,
so a full cache of worst-case CI entries is 1024 x 16 KB = 16 MB of texture
memory - and that is the ceiling for the whole cache, not the delta.

UPLOADS: a palette swap that used to be free now re-decodes and re-uploads the
raster. The decode is a 4096-iteration loop over an L1-resident TLUT; the upload
is at most 16 KB. Against 6 dependent taps on every fragment of a 13.8 Mpx frame,
that is the trade this patch is making, and it is the trade SoH's LUS has always
made - it has no palette path in its shaders at all.

THE A/B LEVER
-------------
`gSohIos.PaletteInShader` = 1 restores the shader path exactly (same bits, same
generated shader, same indexed cache keys) with no rebuild: the `indexed` field
is part of the cache key and `Draw` already re-imports a texture whose
`first.indexed` disagrees with this draw's decision, so the two populations
coexist and a mid-session flip is safe. Diagnostic only - deliberately NOT in
Settings > iOS. It exists so a device read can price the change in one line
instead of two builds.

REV 2 (round 20) - THE RUN-TIME PALETTE RING. The open half of rev 1 turned out
to be real, and the user's 0.0.0.14 device line named it (M-032 §1):

    tex_up_n=8712 tex_up_ms=373.8   (next window 7932 / 383.4)

~1000 texture uploads and ~45 ms of upload time per SECOND of sprite-heavy play.
The cause is the address in the cache key. PM64 shades a sprite by building a
two-tone 16-entry palette per shaded component into a 512-ENTRY RING of buffers
and loading it as a TLUT (`src/port/patches/SpritePatches.c`: the palette is
filled from the component's own palette with one shadow and one highlight
colour, `gDPLoadTLUT_pal16`, then `gDPInvalTexByPalette`, with
`sShadingPaletteIdx` advancing per component). So the same component under the
same light is the SAME 32 BYTES at a DIFFERENT ADDRESS every frame - and rev 1's
key, which keys CI4 on `palette_bank_dram_addr[palette & 15]`, saw 512 distinct
palettes and re-decoded and re-uploaded the raster for each.

REV 2 KEYS ON THE TLUT'S CONTENTS. `PbPaletteContentHash` is FNV-1a over exactly
the bytes a CPU decode reads - the 32-byte bank `ImportTextureCi4` selects, or
CI8's two 256-byte halves - with the length mixed in. For a CPU-decoded CI entry
that hash REPLACES `palette_addrs[]` and `palette_index` in the key (replaces,
not joins: keeping the address would preserve the very miss this fixes, and the
hash already covers the exact bank the index selects). A 0 hash means the palette
was unavailable at import time and the old address key stands.

Option (ii) was considered and rejected: keep the shader path for run-time
(unnamed) palettes and CPU-decode only the static ones. `TilePaletteIsNamed()`
is already in the fork so it was the cheaper change - but it gives back the
shader win on exactly the sprites that dominate a Paper Mario frame, and it
leaves two shader populations and two cache-key shapes live at once. Content
hashing keeps one path everywhere and costs at most 512 L1-resident bytes of
FNV per import - which is nothing against a decode loop of up to 4096 texels,
let alone against the upload it avoids.

INVALIDATION GETS SIMPLER, NOT MORE FRAGILE. With a content key, a TLUT rewritten
at the same address produces a DIFFERENT key: the new content decodes once and
the stale entry is simply never looked up again (LRU reclaims it). So
`G_INVAL_TEX_BY_PAL` has nothing to do - and since it fires once per shaded
component per frame and walked a 1024-entry map each time,
`TextureCacheDeleteByPalette` now returns immediately until an address-keyed
entry has actually been inserted (`mPbPaletteAddrKeyed`, monotonic: a permission
to skip the walk, never a reason to skip a needed deletion).

The correctness argument rests on the hash covering exactly the bytes the decode
reads, which is why the helper mirrors `ImportTextureCi4`/`Ci8` line for line
rather than hashing `palette_staging` wholesale.

VERIFICATION: magenta positive control (every CPU-decoded CI texel forced to
magenta) proved the new path is the one drawing before any real decode was
trusted; oracle and simulator captures before/after at SSAA 1.0 and 2.0,
including a palette-swap beat. See the measurement log M-031, the design notes D31. Rev 2:
`tex_up_n`/`tex_up_ms` in a sprite-heavy scene before and after, plus pixel
parity against mode 1 at SSAA 1.0 and 2.0 (M-032 §4, D32 §3). The A/B lever for
rev 2 alone is `gSohIos.PaletteKeyByContent` (default 1; 0 = rev 1's address
keying), so the two keyings are one binary apart, like rev 1's own lever.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0036-lus-ci-decode-on-cpu.patch"
LUS = "external/libultraship"

EDITS = []

# ------------------------------------------------- include/fast/interpreter.h
H_OLD = """    // Async texture loading (gEnhancements.Graphics.AsyncTextureLoad). When on (and alt
"""
H_NEW = """    // PAPERBOAT (overlay 0036): GPU palettization is a runtime choice, not the only
    // path. Latched once per frame in Run() from `gSohIos.PaletteInShader`; at its
    // default 0 no tile is palettized, the TEXEL*_PALETTE shader bits are never set,
    // `paletteSample` is not emitted into the generated shader at all, and every CI
    // tile takes the CPU decode this file already has for masked/blended/mip tiles.
    bool mPaletteInShader = false;

    // Async texture loading (gEnhancements.Graphics.AsyncTextureLoad). When on (and alt
"""

# REV 2: the cache key carries a hash of the TLUT CONTENTS, so the 512-entry ring
# of run-time sprite-shading palettes (PM64 writes a fresh 16-entry two-tone
# palette per shaded component and rotates through 512 buffers) hits the cache
# instead of re-decoding an identical palette at a different address.
H_KEY_OLD = """    // 1 = uploaded as a CI index texture (palette applied in the shader). Indexed
    // entries do not key on palette contents, which is what makes TLUT swaps free.
    uint8_t indexed;

    bool operator==(const TextureCacheKey&) const noexcept = default;
"""
H_KEY_NEW = """    // 1 = uploaded as a CI index texture (palette applied in the shader). Indexed
    // entries do not key on palette contents, which is what makes TLUT swaps free.
    uint8_t indexed;
    // PAPERBOAT (overlay 0036 rev 2): FNV-1a over exactly the TLUT bytes a CPU
    // decode reads (32 for a CI4 bank, 512 for CI8's two halves). Non-zero
    // REPLACES palette_addrs/palette_index in the key for a CPU-decoded CI
    // entry: the same raster through the same palette CONTENTS is one entry
    // whatever address the TLUT was loaded from, which is what makes PM64's
    // run-time shading-palette ring cheap. 0 for every other entry.
    uint64_t palette_hash;

    bool operator==(const TextureCacheKey&) const noexcept = default;
"""

H_DECL_OLD = """    bool TextureCacheLookup(int i, const TextureCacheKey& key);
"""
H_DECL_NEW = """    bool TextureCacheLookup(int i, const TextureCacheKey& key);
    // PAPERBOAT (overlay 0036 rev 2): hash of the TLUT bytes this tile's CPU
    // decode will read; 0 when the palette is not available, in which case the
    // key falls back to the DRAM-address form exactly as before.
    uint64_t PbPaletteContentHash(uint8_t siz, uint8_t paletteIndex) const;
"""

H_CNT_OLD = """    bool mPaletteInShader = false;
"""
H_CNT_NEW = """    bool mPaletteInShader = false;

    // REV 2: content-hash the TLUT into the cache key (default 1). The A/B
    // lever for rev 2 on its own: at 0 a CPU-decoded CI entry keys on the
    // palette's DRAM ADDRESS, which is rev 1 exactly, so `tex_up_n` can be read
    // in both keyings from one binary. Latched per frame beside mPaletteInShader.
    bool mPaletteKeyByContent = true;

    // REV 2: how many cache entries have ever been inserted keyed on a palette
    // DRAM ADDRESS rather than on its contents. While it is 0,
    // TextureCacheDeleteByPalette cannot possibly match anything, and PM64 emits
    // a G_INVAL_TEX_BY_PAL per shaded component per frame - a full walk of a
    // 1024-entry map each time. Monotonic on purpose: it is a permission to skip
    // the walk, never a reason to skip a deletion that might be needed.
    uint32_t mPbPaletteAddrKeyed = 0;
"""

EDITS.append((f"{LUS}/include/fast/interpreter.h",
              [(H_OLD, H_NEW, 1), (H_KEY_OLD, H_KEY_NEW, 1),
               (H_DECL_OLD, H_DECL_NEW, 1), (H_CNT_OLD, H_CNT_NEW, 1)]))

# ------------------------------------------------- src/fast/interpreter.cpp
# 1. the per-frame latch, next to the other per-frame CVar latches in Run().
CPP_LATCH_OLD = """    // Async texture loading: decode HD/replacement textures off-thread, render vanilla until ready.
    mAsyncTextureLoad = Ship::Context::GetRawInstance()->GetConsoleVariables()->GetInteger(
                            "gEnhancements.Graphics.AsyncTextureLoad", 0) != 0;
"""
CPP_LATCH_NEW = """    // Async texture loading: decode HD/replacement textures off-thread, render vanilla until ready.
    mAsyncTextureLoad = Ship::Context::GetRawInstance()->GetConsoleVariables()->GetInteger(
                            "gEnhancements.Graphics.AsyncTextureLoad", 0) != 0;

    // OVERLAY 0036: do the CI4/CI8 palette lookup in the FRAGMENT SHADER? Default no.
    // The shader path costs a dependent texture tap per filter tap - three under PM64's
    // default three-point filter, four under bilinear, doubled in two-cycle mode - on
    // nearly every fragment of nearly every frame, because Paper Mario is almost entirely
    // CI art. Decoding to RGBA once at upload (which this file already does for every CI
    // tile that cannot be palettized) makes it one hardware-filtered sample. Diagnostic
    // A/B lever, not a menu item; see the patch header for what it trades.
    mPaletteInShader = Ship::Context::GetRawInstance()->GetConsoleVariables()->GetInteger(
                           "gSohIos.PaletteInShader", 0) != 0;

    // OVERLAY 0036 REV 2: key a CPU-decoded CI texture on the TLUT's CONTENTS
    // (default 1) or on the DRAM address it was loaded from (0 = rev 1). The
    // ring of run-time sprite-shading palettes is the reason; see the patch
    // header. Diagnostic A/B lever, not a menu item.
    mPaletteKeyByContent = Ship::Context::GetRawInstance()->GetConsoleVariables()->GetInteger(
                               "gSohIos.PaletteKeyByContent", 1) != 0;
"""

# 2. the decision itself.
CPP_DRAW_OLD = """    bool palettized[2] = { false, false };
    for (int i = 0; i < 2; i++) {
        uint32_t pal_tile = mRdp->first_tile_index + i;
"""
CPP_DRAW_NEW = """    bool palettized[2] = { false, false };
    // OVERLAY 0036: ...and only when `gSohIos.PaletteInShader` says so. With it at its
    // default 0 this loop does not run, no TEXEL*_PALETTE bit is set, the generated
    // shader carries no palette code (the template guards it with
    // `@if(o_palette[0] || o_palette[1])`), the palette texture is never acquired or
    // bound, `mImportIndexed` stays false, and ImportTexture builds the NON-indexed
    // cache key - which already keys CI4 on palette_bank_dram_addr[palette & 15] and
    // CI8 on palette_dram_addr[0..1], so a TLUT swap on the same raster is a distinct
    // entry and decodes again. That is the whole mechanism; see the patch header.
    for (int i = 0; i < 2 && mPaletteInShader; i++) {
        uint32_t pal_tile = mRdp->first_tile_index + i;
"""

# --- rev 2 ---------------------------------------------------------------
CPP_HASH_OLD = """// Invalidate cache entries whose key references the given palette DRAM addr.
// The freed texture ids are recycled on the *next* frame (deferred_free_texture_ids):
// reusing them in this frame would let a deferred backend's command encoder
// (Metal/Vulkan/D3D) sample the later draw's re-uploaded content from an earlier draw.
void Interpreter::TextureCacheDeleteByPalette(const uint8_t* palAddr) {
    for (auto it = mTextureCache.map.begin(); it != mTextureCache.map.end();) {
"""
CPP_HASH_NEW = """// OVERLAY 0036 REV 2: a hash of exactly the TLUT bytes this tile's CPU decode
// will read - the 32-byte bank ImportTextureCi4 selects with palIdx, or CI8's
// two 256-byte halves. FNV-1a over at most 512 L1-resident bytes; the length is
// mixed in so a CI4 bank and a CI8 pair can never collide by construction.
// Returns 0 for "no hash available", which leaves the DRAM-address key in place.
//
// WHY CONTENTS AND NOT THE ADDRESS. PM64 shades sprites by building a two-tone
// 16-entry palette per shaded component into a 512-entry RING of buffers
// (src/port/patches/SpritePatches.c: gDPLoadTLUT_pal16 then
// gDPInvalTexByPalette, sShadingPaletteIdx advancing every component), so the
// SAME component under the SAME light is the same 32 bytes at a different
// address every frame. Keyed on the address, every one of those was a fresh
// decode and a fresh upload of the raster - `tex_up_n` ~1000/s and `tex_up_ms`
// ~45 ms per second of play on the user's 0.0.0.14 phone (M-032 §1). Keyed on the
// contents they are one cache entry, and the invalidation stops mattering: if
// the bytes at an address change, the key changes, so the old entry is simply
// not looked up again (and LRU reclaims it) while the new content decodes once.
uint64_t Interpreter::PbPaletteContentHash(uint8_t siz, uint8_t paletteIndex) const {
    const uint8_t* pbSpans[2] = { nullptr, nullptr };
    uint32_t pbLens[2] = { 0, 0 };
    if (siz == G_IM_SIZ_4b) {
        if (paletteIndex > 15) {
            return 0; // nonsense tile state: keep the old key
        }
        const uint8_t* pbHalf = mRdp->palettes[paletteIndex / 8];
        if (pbHalf == nullptr) {
            return 0;
        }
        pbSpans[0] = pbHalf + (paletteIndex % 8) * 16 * 2;
        pbLens[0] = 16 * 2;
    } else {
        if (mRdp->palettes[0] == nullptr || mRdp->palettes[1] == nullptr) {
            return 0;
        }
        pbSpans[0] = mRdp->palettes[0];
        pbLens[0] = 128 * 2;
        pbSpans[1] = mRdp->palettes[1];
        pbLens[1] = 128 * 2;
    }
    uint64_t pbHash = 1469598103934665603ull;
    for (int pbK = 0; pbK < 2; pbK++) {
        for (uint32_t pbB = 0; pbB < pbLens[pbK]; pbB++) {
            pbHash ^= (uint64_t)pbSpans[pbK][pbB];
            pbHash *= 1099511628211ull;
        }
    }
    pbHash ^= (uint64_t)pbLens[0] | ((uint64_t)pbLens[1] << 32);
    pbHash *= 1099511628211ull;
    return pbHash == 0 ? 1ull : pbHash; // 0 is reserved for "no hash"
}

// Invalidate cache entries whose key references the given palette DRAM addr.
// The freed texture ids are recycled on the *next* frame (deferred_free_texture_ids):
// reusing them in this frame would let a deferred backend's command encoder
// (Metal/Vulkan/D3D) sample the later draw's re-uploaded content from an earlier draw.
void Interpreter::TextureCacheDeleteByPalette(const uint8_t* palAddr) {
    // OVERLAY 0036 REV 2: with content-keyed CI entries nothing in the map holds
    // a palette DRAM address, so this walk can only ever match nothing - and
    // PM64 emits one G_INVAL_TEX_BY_PAL per shaded component per frame. Skip it
    // until an address-keyed entry has actually been inserted (the fallback path
    // when a palette is unavailable at import time).
    if (mPbPaletteAddrKeyed == 0) {
        return;
    }
    for (auto it = mTextureCache.map.begin(); it != mTextureCache.map.end();) {
"""

CPP_KEY_OLD = """    key.mip_levels = mCurrentMipExtraLevels;
    if (mImportIndexed) {
"""
CPP_KEY_NEW = """    key.mip_levels = mCurrentMipExtraLevels;
    // OVERLAY 0036 REV 2: a CPU-decoded CI entry keys on the TLUT's CONTENTS.
    // The address form above is correct but far too fine: PM64's sprite shading
    // writes an identical two-tone palette into a different slot of a 512-entry
    // ring every frame, so address keying re-decoded and re-uploaded the same
    // raster ~1000 times a second (M-032 §1). Replacing the addresses - rather
    // than adding to them - is what turns the ring into one entry;
    // palette_index goes too, since the hash already covers the exact bank it
    // selects. A 0 hash means the palette was not available, and the key stays
    // exactly as it was.
    if (fmt == G_IM_FMT_CI && !mImportIndexed && mPaletteKeyByContent) {
        const uint64_t pbPalHash = PbPaletteContentHash(siz, paletteIndex);
        if (pbPalHash != 0) {
            key.palette_addrs[0] = nullptr;
            key.palette_addrs[1] = nullptr;
            key.palette_index = 0;
            key.palette_hash = pbPalHash;
        }
    }
    if (mImportIndexed) {
"""

CPP_INSERT_OLD = """    it = mTextureCache.map.insert(std::make_pair(key, TextureCacheValue())).first;
"""
CPP_INSERT_NEW = """    it = mTextureCache.map.insert(std::make_pair(key, TextureCacheValue())).first;
    // OVERLAY 0036 REV 2: an entry keyed on a palette DRAM address is the only
    // thing TextureCacheDeleteByPalette can match, so note that one now exists.
    if (key.palette_addrs[0] != nullptr || key.palette_addrs[1] != nullptr) {
        mPbPaletteAddrKeyed++;
    }
"""

EDITS.append((f"{LUS}/src/fast/interpreter.cpp",
              [(CPP_LATCH_OLD, CPP_LATCH_NEW, 1),
               (CPP_DRAW_OLD, CPP_DRAW_NEW, 1),
               (CPP_HASH_OLD, CPP_HASH_NEW, 1),
               (CPP_KEY_OLD, CPP_KEY_NEW, 1),
               (CPP_INSERT_OLD, CPP_INSERT_NEW, 1)]))

chunks = []
for rel, subs in EDITS:
    src = VENDOR / rel
    orig = src.read_text()
    text = orig
    for old, new, expect in subs:
        n = text.count(old)
        assert n == expect, f"{rel}: expected {expect} match(es) of {old!r:.70}, got {n}"
        text = text.replace(old, new, 1)
    with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
         tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
        fa.write(orig); fb.write(text); fa.flush(); fb.flush()
        r = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}",
                            fa.name, fb.name], capture_output=True)
    assert r.returncode == 1, f"{rel}: diff produced no change"
    chunks.append(r.stdout.decode())

OUT.write_text(__doc__ + "\n" + "".join(chunks))
print(f"wrote {OUT}")
