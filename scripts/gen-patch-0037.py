#!/usr/bin/env python3
"""Overlay patch 0037 - a real `DeleteTexture`, and a BYTE budget beside the
1024-entry count cap.

CLASS: (b) upstream bug fix worth sending to JeodC/libultraship (the SoH port
carries the same fix as its own overlay 0020; the bug is in Kenix3's tree too).
Not iOS-gated: the macOS oracle must behave the same way, and the budget lever
has to exist there for an A/B.

THE BUG
-------
`GfxRenderingAPIMetal::DeleteTexture` is an EMPTY FUNCTION BODY. Fast3D's
texture cache evicts by entry count (`TEXTURE_CACHE_MAX_SIZE = 1024`), hands the
freed id back to `free_texture_ids`, and calls `DeleteTexture` - which does
nothing at all. The `MTL::Texture` therefore stays resident until that id is
reused by a texture of DIFFERENT dimensions (UploadTexture's release-and-
recreate branch). So:

  * eviction frees no GPU memory, ever;
  * the only bound on resident texture memory is the entry COUNT, and an entry
    can be 16 KB (an N64 CI tile) or 64 MB (a 4096x4096 HD replacement);
  * the sampler and the MSAA texture of an evicted slot are never released
    either.

On stock PM64 assets this is invisible (M-025 measured `tex_up_ms` at well under
0.5 % of frame time, which is why it was deferred in D26). It stops being
invisible the moment an Alternate Assets pack lands: MasterKillua's official
PM64 HD/4K O2R pack is megabytes per texture, and 1024 entries of that is
gigabytes of pinned GPU memory - i.e. jetsam (a silent SIGKILL), which is
exactly how the SoH port's 4K pack crash presented on device (its 0020 header).

0036 rev 2 raised the stakes independently: with CI textures decoded on the CPU
and keyed on the TLUT's contents, one raster can now hold SEVERAL entries (one
per palette it is drawn with), so the count cap fills faster than it used to.

THE FIX, IN THREE PARTS
-----------------------
1. `DeleteTexture` actually releases: the `MTL::Texture`, the MSAA texture and
   the `MTL::SamplerState`, and it subtracts the slot's bytes from a ledger.
2. A byte ledger in the Metal backend, maintained at the two places a texture is
   created (`UploadTexture`, `UploadTextureMip`) and the one place it is
   destroyed. `GfxRenderingAPI::GetTextureBytes()` is virtual with a default of
   0, so every other backend is unaffected AND the budget disables itself there.
3. One eviction loop in `Interpreter::TextureCacheLookup`, replacing the count
   cap's `if` with a `while` that satisfies BOTH bounds: the entry count as
   before, and resident bytes under `gSohIos.TexCacheBudgetMB`. Every eviction
   now calls `DeleteTexture`, which is what makes the COUNT cap free memory too
   (it never did).

WHY THE EVICTION LOOP LIVES IN TextureCacheLookup AND NOT AT ImportTexture's TAIL
--------------------------------------------------------------------------------
The SoH port evicts after each upload. Here the lookup is the better site and
the reason is this fork's own: it is the ONE place an entry is admitted, so a
single loop covers every path (the other three erase sites - `TextureCacheDelete`,
`TextureCacheDeleteByPalette` and the cache clear - are INVALIDATIONS that hand
the id straight back for reuse, where the release-and-recreate branch of
`UploadTexture` already frees the memory and keeps the ledger truthful). It also
keeps this patch's hunks clear of overlay 0036's, which is a hard constraint
here: `apply-overlay.sh` detects "already applied" by reverse-applying at
fuzz=0, so two patches sharing context lines break on the second build.

GPU SAFETY
----------
Releasing a texture the current frame may still be sampling is safe, and for two
independent reasons:

  * Metal command buffers retain the resources their encoders reference until
    completion. That is the invariant the SoH port's 0020 relies on.
  * This fork additionally defers the ID recycling by a frame
    (`deferred_free_texture_ids`, drained in `Run`/`RunCommands` right after
    `mRapi->StartFrame()`), so an evicted slot cannot be re-uploaded until the
    next frame either.

STRICTER THAN UPSTREAM ON ONE POINT. The existing count cap evicts the LRU front
even when that entry is one of the two CURRENTLY BOUND tiles, and papers over it
by nulling `mRenderingState.mTextures[j]`. With a real release behind it that is
a texture the next draw would sample through a dangling slot, so the loop STOPS
instead: if the LRU front is bound right now, eviction ends for this lookup. The
cache is then allowed to exceed its bound by the couple of entries that are
actually in use, which is the correct trade.

THE BUDGET
----------
`gSohIos.TexCacheBudgetMB`, re-read every 512 lookups (a fraction of a second)
so it can be A/B'd live over the bridge with no rebuild. Default: the shell's
`PBIos_TexCacheBudgetDefaultMB()` on iOS (physical RAM / 4, capped at 1536 MB,
floored at 192 MB - D33), 1536 elsewhere, which is the SoH port's flat default.
A floor of 128 entries is kept whatever the budget says, so a budget set absurdly
low degrades into thrash rather than into a cache that cannot hold the tiles of
one draw.

The counters (`gPBIosTexCache*`) are read by overlay 0012's report - the `fps`
line's `texcache=` group and the bridge's `texcache` verb.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0037-lus-texcache-delete-and-byte-budget.patch"

EDITS = []  # (relative path, old, new, expected count)

# ---------------------------------------------------------------------------
# 1. The backend interface: a byte query with a zero default.
# ---------------------------------------------------------------------------
EDITS.append((
    "external/libultraship/include/fast/backends/gfx_rendering_api.h",
    """    virtual void DeleteTexture(uint32_t texId) = 0;
    virtual void SetTextureFilter(FilteringMode mode) = 0;
""",
    """    virtual void DeleteTexture(uint32_t texId) = 0;
    // OVERLAY 0037: total bytes of live GPU textures created through
    // UploadTexture/UploadTextureMip. A backend without accounting reports 0,
    // which disables the interpreter's byte-budget eviction entirely - so this
    // is additive for every backend but Metal.
    virtual size_t GetTextureBytes() {
        return 0;
    }
    virtual void SetTextureFilter(FilteringMode mode) = 0;
""",
    1,
))

# ---------------------------------------------------------------------------
# 2. The Metal backend: declaration + the ledger member.
# ---------------------------------------------------------------------------
EDITS.append((
    "external/libultraship/include/fast/backends/gfx_metal.h",
    """    void DeleteTexture(uint32_t texId) override;
    void SetTextureFilter(FilteringMode mode) override;
""",
    """    void DeleteTexture(uint32_t texId) override;
    size_t GetTextureBytes() override; // OVERLAY 0037
    void SetTextureFilter(FilteringMode mode) override;
""",
    1,
))

EDITS.append((
    "external/libultraship/include/fast/backends/gfx_metal.h",
    """    std::vector<struct TextureDataMetal> mTextures;
    std::vector<FramebufferMetal> mFramebuffers;
""",
    """    std::vector<struct TextureDataMetal> mTextures;
    // OVERLAY 0037: exact resident bytes of the textures above, maintained by
    // UploadTexture / UploadTextureMip / DeleteTexture. Framebuffer attachments
    // are NOT counted - they are not cache entries and evicting cannot free
    // them.
    size_t mTextureBytes = 0;
    std::vector<FramebufferMetal> mFramebuffers;
""",
    1,
))

# ---------------------------------------------------------------------------
# 3. DeleteTexture: the empty body, and the byte query beside it.
# ---------------------------------------------------------------------------
EDITS.append((
    "external/libultraship/src/fast/backends/gfx_metal.cpp",
    """void GfxRenderingAPIMetal::DeleteTexture(uint32_t texID) {
}
""",
    """// OVERLAY 0037: the bytes one slot's texture occupies. A mip chain adds one
// third over the base level (sum of 1/4^n), which is what the allocator charges.
static size_t PbMetalTextureBytes(MTL::Texture* tex) {
    if (tex == nullptr) {
        return 0;
    }
    size_t bytes = (size_t)tex->width() * (size_t)tex->height() * 4;
    if (tex->mipmapLevelCount() > 1) {
        bytes += bytes / 3;
    }
    return bytes;
}

void GfxRenderingAPIMetal::DeleteTexture(uint32_t texID) {
    // OVERLAY 0037: this was an EMPTY FUNCTION, so cache eviction never freed a
    // byte of GPU memory - the MTLTexture stayed resident until its id was
    // reused by a texture of different dimensions. Releasing here is safe even
    // mid-frame: Metal command buffers retain the resources their encoders
    // reference until completion, and this fork additionally defers the ID
    // recycling by a frame (deferred_free_texture_ids).
    if (texID >= mTextures.size()) {
        return;
    }
    TextureDataMetal* texture_data = &mTextures[texID];
    if (texture_data->texture != nullptr) {
        const size_t bytes = PbMetalTextureBytes(texture_data->texture);
        mTextureBytes = bytes > mTextureBytes ? 0 : mTextureBytes - bytes;
        texture_data->texture->release();
        texture_data->texture = nullptr;
    }
    if (texture_data->msaaTexture != nullptr) {
        texture_data->msaaTexture->release();
        texture_data->msaaTexture = nullptr;
    }
    // The sampler is recreated by SetSamplerParameters, which TextureCacheLookup
    // calls for every entry it admits, so dropping it here cannot leave a slot
    // without one.
    if (texture_data->sampler != nullptr) {
        texture_data->sampler->release();
        texture_data->sampler = nullptr;
    }
    texture_data->mip_levels = 0;
}

size_t GfxRenderingAPIMetal::GetTextureBytes() {
    return mTextureBytes;
}
""",
    1,
))

# ---------------------------------------------------------------------------
# 4. The ledger's two credit sites. Both edits sit strictly BETWEEN overlay
#    0012's two UploadTexture hunks, which is deliberate (see the header).
# ---------------------------------------------------------------------------
EDITS.append((
    "external/libultraship/src/fast/backends/gfx_metal.cpp",
    """    MTL::Texture* texture = texture_data->texture;
    if (texture_data->texture == nullptr || texture_data->texture->width() != width ||
        texture_data->texture->height() != height || texture_data->texture->mipmapLevelCount() != 1) {
        if (texture_data->texture != nullptr)
            texture_data->texture->release();

        texture = mDevice->newTexture(texture_descriptor);
    }
""",
    """    MTL::Texture* texture = texture_data->texture;
    if (texture_data->texture == nullptr || texture_data->texture->width() != width ||
        texture_data->texture->height() != height || texture_data->texture->mipmapLevelCount() != 1) {
        if (texture_data->texture != nullptr) {
            // OVERLAY 0037: debit the ledger for the texture being thrown away.
            const size_t pbOld = PbMetalTextureBytes(texture_data->texture);
            mTextureBytes = pbOld > mTextureBytes ? 0 : mTextureBytes - pbOld;
            texture_data->texture->release();
        }

        texture = mDevice->newTexture(texture_descriptor);
        mTextureBytes += (size_t)width * (size_t)height * 4; // OVERLAY 0037
    }
""",
    1,
))

EDITS.append((
    "external/libultraship/src/fast/backends/gfx_metal.cpp",
    """            if (texture_data->texture != nullptr)
                texture_data->texture->release();

            MTL::TextureDescriptor* texture_descriptor =
                MTL::TextureDescriptor::texture2DDescriptor(MTL::PixelFormatRGBA8Unorm, width, height, true);
            texture_descriptor->setArrayLength(1);
            texture_descriptor->setMipmapLevelCount(totalLevels);
            texture_descriptor->setSampleCount(1);
            texture_descriptor->setStorageMode(MTL::StorageModeShared);
            texture_data->texture = mDevice->newTexture(texture_descriptor);
""",
    """            if (texture_data->texture != nullptr) {
                // OVERLAY 0037: same debit as UploadTexture's.
                const size_t pbOld = PbMetalTextureBytes(texture_data->texture);
                mTextureBytes = pbOld > mTextureBytes ? 0 : mTextureBytes - pbOld;
                texture_data->texture->release();
            }

            MTL::TextureDescriptor* texture_descriptor =
                MTL::TextureDescriptor::texture2DDescriptor(MTL::PixelFormatRGBA8Unorm, width, height, true);
            texture_descriptor->setArrayLength(1);
            texture_descriptor->setMipmapLevelCount(totalLevels);
            texture_descriptor->setSampleCount(1);
            texture_descriptor->setStorageMode(MTL::StorageModeShared);
            texture_data->texture = mDevice->newTexture(texture_descriptor);
            mTextureBytes += PbMetalTextureBytes(texture_data->texture); // OVERLAY 0037
""",
    1,
))

# ---------------------------------------------------------------------------
# 5. The interpreter: one eviction loop, both bounds, and the counters.
# ---------------------------------------------------------------------------
EDITS.append((
    "external/libultraship/include/fast/interpreter.h",
    """    GfxTextureCache mTextureCache{};
""",
    """    GfxTextureCache mTextureCache{};
    // OVERLAY 0037: the byte budget's latched value (bytes; 0 = no budget) and
    // the lookup counter that decides when to re-read the CVar. Latched rather
    // than read per lookup because TextureCacheLookup is the hottest function
    // in the texture path.
    size_t mPbTexBudgetBytes = 0;
    uint32_t mPbTexLookups = 0;
""",
    1,
))

EDITS.append((
    "external/libultraship/src/fast/interpreter.cpp",
    """    if (mTextureCache.map.size() >= TEXTURE_CACHE_MAX_SIZE) {
        // Remove the texture that was least recently used
        it = mTextureCache.lru.front().it;
        mTextureCache.deferred_free_texture_ids.push_back(it->second.texture_id);
        for (int j = 0; j < SHADER_MAX_TEXTURES; j++) {
            if (mRenderingState.mTextures[j] == &*it)
                mRenderingState.mTextures[j] = nullptr;
        }
        mTextureCache.map.erase(it);
        mTextureCache.lru.pop_front();
    }
""",
    """    // OVERLAY 0037: evict until BOTH bounds hold - upstream's entry count, and
    // a BYTE budget the count alone cannot express (an entry is 16 KB of N64 CI
    // tile or 64 MB of 4K replacement). Every eviction now calls DeleteTexture,
    // which is also what finally makes the COUNT cap free memory: the Metal
    // backend's DeleteTexture was an empty function, so an evicted id kept its
    // MTLTexture until it was reused at different dimensions.
    while (!mTextureCache.lru.empty()) {
        const bool pbOverCount = mTextureCache.map.size() >= TEXTURE_CACHE_MAX_SIZE;
        const bool pbOverBytes = mPbTexBudgetBytes != 0 && mRapi->GetTextureBytes() > mPbTexBudgetBytes &&
                                 mTextureCache.map.size() > 128;
        if (!pbOverCount && !pbOverBytes) {
            break;
        }
        // Stricter than upstream: never evict an entry that is BOUND right now.
        // The old code evicted it anyway and papered over it by nulling the
        // rendering-state pointer, which was harmless only because the texture
        // was never actually released. It is released now.
        TextureCacheMap::iterator pbEvict = mTextureCache.lru.front().it;
        bool pbBound = false;
        for (int j = 0; j < SHADER_MAX_TEXTURES; j++) {
            if (mRenderingState.mTextures[j] == &*pbEvict) {
                pbBound = true;
            }
        }
        if (pbBound) {
            break;
        }
        mRapi->DeleteTexture(pbEvict->second.texture_id);
        mTextureCache.deferred_free_texture_ids.push_back(pbEvict->second.texture_id);
        mTextureCache.map.erase(pbEvict);
        mTextureCache.lru.pop_front();
        gPBIosTexCacheEvicts++;
    }
    // Republished here because this is where the numbers move. Sampled before
    // the insert below rather than after it, so this patch's context stays
    // clear of overlay 0036's hunk: `entries` is the count BEFORE the entry
    // this lookup is about to admit.
    gPBIosTexCacheEntries = (uint32_t)mTextureCache.map.size();
    gPBIosTexCacheBytes = (uint64_t)mRapi->GetTextureBytes();
    gPBIosTexCacheBudgetMB = (uint32_t)(mPbTexBudgetBytes / (1024 * 1024));
""",
    1,
))

# The counters and the budget default, defined just above TextureCacheLookup.
EDITS.append((
    "external/libultraship/src/fast/interpreter.cpp",
    """bool Interpreter::TextureCacheLookup(int i, const TextureCacheKey& key) {
""",
    """// OVERLAY 0037: the texture cache's own ledger, read by overlay 0012's report
// (the `fps` line's `texcache=` group and the bridge's `texcache` verb).
// Written on the game thread and read on the main thread, which on iOS is the
// same thread; `volatile` for the same reason gPBIosGpuMs is, and no
// <atomic> include is added to this translation unit for four counters.
// `extern "C"`, and not for the ABI: this translation unit is inside
// `namespace Fast`, so without it the four symbols are mangled into that
// namespace and Engine.cpp's declarations do not resolve.
extern "C" volatile uint64_t gPBIosTexCacheEvicts = 0;
extern "C" volatile uint64_t gPBIosTexCacheBytes = 0;
extern "C" volatile uint32_t gPBIosTexCacheEntries = 0;
extern "C" volatile uint32_t gPBIosTexCacheBudgetMB = 0;

// The default budget in MB. On iOS the shell picks it from the device's
// physical memory (D33); elsewhere it is the SoH port's flat 1536 MB, which is
// also what the oracle gets so an A/B can be run there.
#ifdef __IOS__
extern "C" int PBIos_TexCacheBudgetDefaultMB(void);
#endif
static int32_t PbTexCacheBudgetDefaultMB() {
#ifdef __IOS__
    return (int32_t)PBIos_TexCacheBudgetDefaultMB();
#else
    return 1536;
#endif
}

bool Interpreter::TextureCacheLookup(int i, const TextureCacheKey& key) {
    // OVERLAY 0037: the budget is re-read (and the ledger republished) every 512
    // lookups, HIT OR MISS - a fraction of a second - so `set
    // gSohIos.TexCacheBudgetMB N` over the bridge is a live A/B with no rebuild
    // and `texcache` does not go stale in a scene that is fully cached.
    if ((mPbTexLookups++ % 512) == 0) {
        const int32_t pbBudgetMb = Ship::Context::GetRawInstance()->GetConsoleVariables()->GetInteger(
            "gSohIos.TexCacheBudgetMB", PbTexCacheBudgetDefaultMB());
        mPbTexBudgetBytes = pbBudgetMb > 0 ? (size_t)pbBudgetMb * 1024 * 1024 : 0;
        gPBIosTexCacheEntries = (uint32_t)mTextureCache.map.size();
        gPBIosTexCacheBytes = (uint64_t)mRapi->GetTextureBytes();
        gPBIosTexCacheBudgetMB = (uint32_t)(mPbTexBudgetBytes / (1024 * 1024));
    }
""",
    1,
))


def main():
    originals = {}
    for rel, old, new, count in EDITS:
        path = VENDOR / rel
        text = originals.get(rel)
        if text is None:
            text = path.read_text()
            originals[rel] = text
        n = text.count(old)
        assert n == count, f"{rel}: expected {count} match(es) of\n{old[:120]!r}\ngot {n}"
        originals[rel] = text.replace(old, new, count)

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        chunks = []
        for rel in originals:
            src = VENDOR / rel
            backup = src.read_text()
            (tmp / "orig").write_text(backup)
            (tmp / "new").write_text(originals[rel])
            diff = subprocess.run(
                ["diff", "-u",
                 "--label", f"a/{rel}", "--label", f"b/{rel}",
                 str(tmp / "orig"), str(tmp / "new")],
                capture_output=True, text=True)
            assert diff.returncode == 1, f"{rel}: diff produced no change (rc={diff.returncode})"
            chunks.append(diff.stdout)

    header = __doc__.strip() + "\n\n"
    OUT.write_text(header + "".join(chunks))
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
