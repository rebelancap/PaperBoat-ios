#!/usr/bin/env python3
"""Overlay patch 0033 - the Metal backend stops encoding render passes that draw
nothing.

CLASS: (b) upstream bug fix, worth sending to JeodC/libultraship (and to
Kenix3/libultraship - the same code is in every sibling's LUS). Not iOS-gated:
the macOS oracle is the ground truth and must render the same way.

THE MEASUREMENT THAT MOTIVATED IT (M-028/M-029, the user's iPhone Air, A19-class)
------------------------------------------------------------------------------
    SSAA 2.0 (dims 5472x2520): gpu_game_ms p50=8.60..14.50 p95=22.5 max=41  fps 59
    SSAA 1.5 (dims 4104x1890): gpu_game_ms p50=6.34 p95=8.06
    SSAA 1.0 (dims 2736x1260): gpu_game_ms p50=1.77 p95=3.44

1x costs 1.77 ms. 2x is 4x the pixels, so a pixel-bound frame should land near
7 ms; it lands at 8.6-14.5 with a p95 of 22.5. Something in the 2x frame is
super-linear in resolution and is NOT the shading of the scene. The design notes D28 §2
named three suspects; this patch is (iii), the cheapest and most mechanical of
them.

WHAT THE BUG IS
---------------
`CopyFramebuffer` and `SelectTextureFb` both have to end the framebuffer's live
render encoder - the first so a blit can run on the same command buffer, the
second so the texture is readable by another encoder. Both then IMMEDIATELY open
a fresh render encoder on the same framebuffer, "so any subsequent rendering to
this framebuffer preserves what was drawn".

When there is no subsequent rendering - and at the end of a PM64 frame there
never is - that encoder is a complete render pass that draws nothing. On a TBDR
GPU a render pass is not free just because it issues no draws: `LoadActionLoad`
on colour AND on Depth32Float pulls the whole attachment into tile memory and
`StoreActionStore` writes it all back. At PaperBoat's 2x supersampled
5472x2520 that is 13.8 Mpx x 4 B x 2 attachments, loaded and stored, for a pass
with zero fragments - roughly 220 MB of tile traffic per occurrence.

`StartDrawToFramebuffer` has the same shape and is worse, because it fires on
EVERY frame of every port: it opens a render encoder immediately, and every
caller in `interpreter.cpp` (`:7169`, `:7262`, `:7364`, `:7179`, ...) follows it
straight away with `ClearFramebuffer`, which ends that encoder and opens its own.
The eager encoder therefore never receives a draw, and pays a full load and store
on the way past.

So PM64 pays for three empty full-attachment passes a frame, unconditionally:

  * `StartDrawToFramebuffer` -> `ClearFramebuffer` at the top of every frame;
  * `port_emitPrevFrameCapture` (`src/port/gfx_frame.c:85-86`) appends a
    full-screen `gDPCopyFB` prev-frame mirror to the END of every master display
    list - so `CopyFramebuffer` runs, and nothing is drawn afterwards;
  * the final composite samples the game framebuffer, which goes through
    `SelectTextureFb` - and again nothing more is drawn into the source.

THE FIX: MAKE THE RESTART LAZY
------------------------------
Neither call site knows whether anything will draw next, and it cannot know. So
do not decide at that moment: record that an encoder is OWED
(`mPbRestartPending`), and open it on the first operation that actually needs
one. (`StartDrawToFramebuffer` still creates the command BUFFER eagerly - other
encoders on the framebuffer need it - and defers only the render encoder.)
`PbEnsureEncoder(fb_id)` is that operation, called from the four
places that touch a live encoder - `SetViewport`, `SetScissor`, `DrawTriangles`
and ImGui's `RenderDrawData` - plus the sites that end or replace an encoder,
which handle a pending restart by simply not ending what was never opened.
`EndFrame` deliberately does NOT call it: a restart still owed when the frame
ends is exactly the pass this patch exists to delete, and it is counted there
(`pass_skipped=` on the perf line, overlay 0012 rev 4).

Nothing about ordering or content changes. The lazily-created encoder is created
with the same descriptor, the same load actions, the same viewport and the same
scissor as the eager one, and the per-encoder state resets stay exactly where
they were. The only difference is that an encoder nobody used is never created.

REV 2 - THE LOAD ACTIONS HAD TO BE EXPLICIT (found in review, before any device
saw it). Rev 1 re-opened with the descriptor as it stood, reasoning that its load
actions are `LoadActionLoad` at rest. Overlay 0034, landing in the same round,
made that false for framebuffer 0, whose DEPTH load action it leaves as `Clear`.
And framebuffer 0 is not always just the composite: at SSAA 1.0 with MSAA off and
no post passes, `Interpreter::ViewportMatchesRendererResolution()` holds,
`mRendersToFb` is false (`interpreter.cpp:7126`) and PM64 draws its depth-tested
scene geometry STRAIGHT INTO framebuffer 0. The restart after that frame's
`gDPCopyFB` would then have re-opened the pass with a depth clear, and every
depth-tested draw for the rest of the frame would have tested against far.
Invisible at the 2x default, which is exactly why the sim and device runs of
0.0.0.11 looked perfect.

So the two kinds of deferral are now distinguished. `mPbRestartLoads` marks a
RESTART over content that already exists (copy, texture-select): it forces
`LoadActionLoad` on colour and depth and restores the descriptor afterwards,
precisely as the eager restarts did. The frame's FIRST pass on a framebuffer
(`StartDrawToFramebuffer`) does not, so 0034's screen-depth `Clear` still
happens. Verified at BOTH SSAA 1.0 and 2.0 (M-029 §8).

WHY NOT JUST DROP THE RESTART. Because a frame CAN keep drawing after a copy -
PM64's own scene mirror (`port_emitSceneMirrorCapture`) is captured mid-display
list and its consumers redraw the region they sampled. Dropping the restart
would lose those draws; deferring it cannot.

REV 3 - THE DEVICE CRASH REV 1 AND REV 2 BOTH SHIPPED (M-032 §2, D32 §1).
The user's 0.0.0.14, about four minutes into play: SIGABRT on the main thread,
2026-09-18T05:43:48Z. Symbolicated against the staged IPA:

    Fast::GfxRenderingAPIMetal::DrawTriangles(...) + 2192      <- the epilogue,
    Fast::Interpreter::GfxSpTri1(...) + 3920                      right after
    Fast::Interpreter::GfxDrawRectangle(...) + 716                the LOCAL pool
    Fast::Interpreter::GfxDpTextureRectangle(...) + 240           release
    Fast::gfx_tex_rect_wide_handler_custom(...) + 220
    Fast::Interpreter::Run(...) + 1516
    ... libc++abi terminate <- _objc_terminate  (uncaught ObjC exception)

`renderCommandEncoder()` returns an AUTORELEASED encoder. Upstream's two eager
creations (`StartDrawToFramebuffer`, `ClearFramebuffer`) run with the FRAME pool
(`mFrameAutoreleasePool`, created in StartFrame, released at the end of
EndFrame) innermost, so their encoders live as long as the frame. Rev 1 moved
the creation into `PbEnsureEncoder`, which is called from whoever needs an
encoder FIRST - and `DrawTriangles` wraps its whole body in a LOCAL autorelease
pool (`gfx_metal.cpp:809`, `PbEnsureEncoder` at :812). So when the first
consumer of a pending restart was a draw - a texrect right after PM64's
per-frame `gDPCopyFB`, with no `SetViewport`/`SetScissor` in between - the
encoder was released at that draw's own pool drain, while still open. Metal
raises "Command encoder released without endEncoding" as an ObjC exception;
nothing catches it; `abort()`. And had it survived, every later use that frame
would have been on a freed object.

THE SIMULATOR CANNOT SEE THIS BUG, AND THAT IS WORTH WRITING DOWN. Neither the
simulator nor the macOS oracle ever asserted, which is why three rounds of
verification missed it. Round 20 tried to reproduce it deliberately: a build
with the retain REMOVED, run under the Metal API validation layer
(`METAL_DEVICE_WRAPPER_TYPE=1 MTL_DEBUG_LAYER=1`), reached the title screen and
ran five minutes of attract demo with 72,000 validation lines and NOT ONE
"Command encoder released without endEncoding" (M-032 §3). The simulator's Metal
runtime simply does not enforce encoder lifetime. (The layer must also run in
`MTL_DEBUG_LAYER_ERROR_MODE=nslog`: in its default assert mode it kills this app
in the first frame over upstream LUS's unconditional `setDepthClipMode(Clamp)`,
which the simulator GPU family does not advertise.)

So the evidence for rev 3 is the symbolicated device backtrace plus the code
reading above, not a reproduction; what the simulator can and does say is that
rev 3 is not a regression - title screen 0.977, validation layer on, zero
encoder complaints. The layer is now on by default for every `run-sim.sh` all
the same: it costs nothing here and it is the only instrument that could ever
catch the next one.

The fix is one retain and one release: `PbEnsureEncoder` retains the encoder and
records it in `mPbRetainedEncoders`; `EndFrame` releases every entry and clears
the vector, immediately after `mFrameAutoreleasePool->release()`. That gives a
deferred encoder exactly the lifetime the eager ones had. No other site releases
an encoder (the pool always did), so there is nothing to double-release:
`ClearFramebuffer`, `ResolveMSAAColorBuffer`, `ReadFramebufferToCPU`,
`CopyFramebuffer` and `SelectTextureFb` only ever call `endEncoding()` and drop
the pointer. `endEncoding()` has already been called on all of them by the loop
at the top of EndFrame before the release runs.

VERIFICATION: sim title-screen frames classified identical to the reference at
SSAA 2.0 AND at SSAA 1.0 (the mode that exercises the rev-2 path), oracle
re-linked and run, and `pass_skipped` on the perf line reads >= 1 per frame -
the passes that are no longer encoded at all. Rev 3: sim title 0.977 with the
Metal validation layer on and zero encoder complaints, and the same layer run
against a retain-less build to confirm the simulator cannot reproduce the device
abort at all (M-032 §3).
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0033-lus-metal-lazy-encoder-restart.patch"
LUS = "external/libultraship"

EDITS = []

# ------------------------------------------------- include/fast/backends/gfx_metal.h
H_FIELD_OLD = """    // When true, command buffer is created on the readback queue (no enqueue ordering).
    // First ReadFramebufferToCPU returns zeros and flips this; real data from frame 2+.
    bool mUseReadbackQueue = false;
};
"""
H_FIELD_NEW = """    // When true, command buffer is created on the readback queue (no enqueue ordering).
    // First ReadFramebufferToCPU returns zeros and flips this; real data from frame 2+.
    bool mUseReadbackQueue = false;

    // PAPERBOAT (overlay 0033): a render encoder that CopyFramebuffer /
    // SelectTextureFb owe this framebuffer but have not opened, because nothing
    // has needed one yet. Opened on demand by PbEnsureEncoder; if the frame ends
    // with it still owed, the pass is simply never encoded - which is the point,
    // since an empty render pass still loads and stores every attachment.
    bool mPbRestartPending = false;
    // REV 2: true when the owed encoder is a RESTART over content that already
    // exists (a copy or a texture-select ended the live one), as opposed to the
    // frame's FIRST pass on this framebuffer. A restart must LOAD colour and
    // depth whatever the descriptor says at rest; the first pass must not, or
    // overlay 0034's screen-depth Clear never happens.
    bool mPbRestartLoads = false;
};
"""

H_DECL_OLD = """  private:
    bool NonUniformThreadGroupSupported();
    void SetupScreenFramebuffer(uint32_t width, uint32_t height);
"""
H_DECL_NEW = """  private:
    bool NonUniformThreadGroupSupported();
    void SetupScreenFramebuffer(uint32_t width, uint32_t height);
    // PAPERBOAT (overlay 0033): open a render encoder this framebuffer is owed.
    // No-op unless a copy or a texture-select deferred one.
    void PbEnsureEncoder(int fb_id);
    // REV 3: the encoders PbEnsureEncoder opened this frame, retained so they
    // outlive the LOCAL autorelease pool of whoever happened to need one first
    // (DrawTriangles has one). Released in EndFrame. See the patch header.
    std::vector<MTL::RenderCommandEncoder*> mPbRetainedEncoders;
"""

EDITS.append((f"{LUS}/include/fast/backends/gfx_metal.h",
              [(H_FIELD_OLD, H_FIELD_NEW, 1), (H_DECL_OLD, H_DECL_NEW, 1)]))

# ------------------------------------------------- src/fast/backends/gfx_metal.cpp
CPP_DEF_OLD = """void GfxRenderingAPIMetal::StartDrawToFramebuffer(int fb_id, float noise_scale) {
"""
CPP_DEF_NEW = """// PAPERBOAT (overlay 0033): open the render encoder a copy or a texture-select
// deferred, if one is owed. This is the whole of the lazy-restart mechanism: the
// callers below that used to re-open an encoder immediately now only record that
// one is owed, and an encoder nobody asks for is never created - so an empty
// render pass never loads and stores 13.8 Mpx of colour and depth for nothing.
//
// Everything here is what CopyFramebuffer's restart used to do inline: the same
// descriptor, the same load actions, the same depth clip mode, the same viewport
// and scissor. The per-encoder state resets stay at the deferral sites, where
// they already were.
//
// REV 2 - THE LOAD ACTIONS ARE NOT OPTIONAL, AND THIS COST A BUG. Rev 1 used the
// descriptor as it stood, on the reasoning that its load actions are
// LoadActionLoad at rest. That stopped being true in the same round: overlay
// 0034 leaves framebuffer 0's DEPTH load action as Clear. At SSAA 1.0 with MSAA
// off, Interpreter::ViewportMatchesRendererResolution() holds, `mRendersToFb` is
// false (interpreter.cpp:7126) and PM64 therefore draws depth-tested scene
// geometry straight into framebuffer 0 - so the restart after its per-frame
// gDPCopyFB would have re-opened the pass with a depth CLEAR and every
// depth-tested draw for the rest of that frame would have tested against far.
// Invisible at SSAA > 1, where framebuffer 0 is only the composite, which is why
// a sim run at the 2x default looked perfect. Found in review, not in testing.
//
// So a RESTART (mPbRestartLoads) forces Load on colour and depth and restores
// the descriptor afterwards, exactly as the eager restarts did; the frame's
// FIRST pass does not, so 0034's screen-depth Clear still happens.
void GfxRenderingAPIMetal::PbEnsureEncoder(int fb_id) {
    if (fb_id < 0 || fb_id >= (int)mFramebuffers.size()) {
        return;
    }
    FramebufferMetal& fb = mFramebuffers[fb_id];
    if (!fb.mPbRestartPending) {
        return;
    }
    const bool pbLoads = fb.mPbRestartLoads;
    fb.mPbRestartPending = false;
    fb.mPbRestartLoads = false;
    if (fb.mCommandBuffer == nullptr || fb.mRenderPassDescriptor == nullptr) {
        return;
    }

    MTL::RenderPassColorAttachmentDescriptor* pbColor = fb.mRenderPassDescriptor->colorAttachments()->object(0);
    MTL::RenderPassDepthAttachmentDescriptor* pbDepth = fb.mRenderPassDescriptor->depthAttachment();
    MTL::LoadAction pbOrigColor = pbColor->loadAction();
    MTL::LoadAction pbOrigDepth = MTL::LoadActionDontCare;
    if (pbLoads) {
        pbColor->setLoadAction(MTL::LoadActionLoad);
        if (fb.mHasDepthBuffer && pbDepth != nullptr) {
            pbOrigDepth = pbDepth->loadAction();
            pbDepth->setLoadAction(MTL::LoadActionLoad);
        }
    }

    fb.mCommandEncoder = fb.mCommandBuffer->renderCommandEncoder(fb.mRenderPassDescriptor);
    // REV 3 - THE LIFETIME. renderCommandEncoder() returns an AUTORELEASED
    // encoder, owned by whichever autorelease pool is innermost right now.
    // Upstream's eager restarts ran under the FRAME pool, so their encoders
    // lived until EndFrame released it; this one is opened from whoever needs
    // it first, and DrawTriangles wraps its whole body in a LOCAL pool. When
    // the first consumer of a pending restart was a draw, the encoder was
    // released - still open - at that draw's pool drain, Metal raised
    // "Command encoder released without endEncoding", and the uncaught ObjC
    // exception aborted the app (M-032 §2: SIGABRT in DrawTriangles's epilogue
    // on the user's 0.0.0.14). Retain it here and release it in EndFrame, which
    // gives the deferred encoder exactly the frame-pool lifetime the eager
    // encoders had. Nothing else releases these - the pool used to.
    fb.mCommandEncoder->retain();
    mPbRetainedEncoders.push_back(fb.mCommandEncoder);
    std::string fbce_label = fmt::format("FrameBuffer {} Command Encoder ({})", fb_id,
                                         pbLoads ? "deferred restart" : "deferred first pass");
    fb.mCommandEncoder->setLabel(NS::String::string(fbce_label.c_str(), NS::UTF8StringEncoding));
    fb.mCommandEncoder->setDepthClipMode(MTL::DepthClipModeClamp);
    fb.mCommandEncoder->setViewport(*fb.mViewport);
    fb.mCommandEncoder->setScissorRect(*fb.mScissorRect);
    fb.mHasEndedEncoding = false;

    if (pbLoads) {
        pbColor->setLoadAction(pbOrigColor);
        if (fb.mHasDepthBuffer && pbDepth != nullptr) {
            pbDepth->setLoadAction(pbOrigDepth);
        }
    }
}

void GfxRenderingAPIMetal::StartDrawToFramebuffer(int fb_id, float noise_scale) {
"""

# --- the four consumers of a live encoder -------------------------------------
CPP_STARTDRAW_OLD = """        fb.mCommandEncoder = fb.mCommandBuffer->renderCommandEncoder(fb.mRenderPassDescriptor);
        std::string fbce_label = fmt::format("FrameBuffer {} Command Encoder", fb_id);
        fb.mCommandEncoder->setLabel(NS::String::string(fbce_label.c_str(), NS::UTF8StringEncoding));
        fb.mCommandEncoder->setDepthClipMode(MTL::DepthClipModeClamp);
    }
"""
CPP_STARTDRAW_NEW = """        // OVERLAY 0033: the COMMAND BUFFER is created eagerly (other encoders on
        // this framebuffer need it); the render ENCODER is not. Every caller in
        // interpreter.cpp follows StartDrawToFramebuffer immediately with
        // ClearFramebuffer (:7169, :7262, :7364, ...), which ends this encoder
        // and opens its own - so the eager one drew nothing and yet loaded and
        // stored the whole colour and Depth32Float attachment. At PaperBoat's 2x
        // supersampled 5472x2520 that is ~220 MB of tile traffic per frame for a
        // pass with zero fragments.
        fb.mPbRestartPending = true;
        fb.mPbRestartLoads = false; // rev 2: the frame's first pass on this fb
        fb.mHasEndedEncoding = true;
    }
"""

CPP_VIEWPORT_OLD = """void GfxRenderingAPIMetal::SetViewport(int x, int y, int width, int height) {
    FramebufferMetal& fb = mFramebuffers[mCurrentFramebuffer];

    fb.mViewport->originX = x;
"""
CPP_VIEWPORT_NEW = """void GfxRenderingAPIMetal::SetViewport(int x, int y, int width, int height) {
    PbEnsureEncoder(mCurrentFramebuffer); // overlay 0033
    FramebufferMetal& fb = mFramebuffers[mCurrentFramebuffer];

    fb.mViewport->originX = x;
"""

CPP_SCISSOR_OLD = """void GfxRenderingAPIMetal::SetScissor(int x, int y, int width, int height) {
    FramebufferMetal& fb = mFramebuffers[mCurrentFramebuffer];
    TextureDataMetal tex = mTextures[fb.mTextureId];
"""
CPP_SCISSOR_NEW = """void GfxRenderingAPIMetal::SetScissor(int x, int y, int width, int height) {
    PbEnsureEncoder(mCurrentFramebuffer); // overlay 0033
    FramebufferMetal& fb = mFramebuffers[mCurrentFramebuffer];
    TextureDataMetal tex = mTextures[fb.mTextureId];
"""

CPP_DRAW_OLD = """void GfxRenderingAPIMetal::DrawTriangles(float buf_vbo[], size_t buf_vbo_len, size_t buf_vbo_num_tris) {
    NS::AutoreleasePool* autorelease_pool = NS::AutoreleasePool::alloc()->init();
    bool textures_changed = false;

    auto& current_framebuffer = mFramebuffers[mCurrentFramebuffer];
"""
CPP_DRAW_NEW = """void GfxRenderingAPIMetal::DrawTriangles(float buf_vbo[], size_t buf_vbo_len, size_t buf_vbo_num_tris) {
    NS::AutoreleasePool* autorelease_pool = NS::AutoreleasePool::alloc()->init();
    bool textures_changed = false;

    PbEnsureEncoder(mCurrentFramebuffer); // overlay 0033 - a draw needs a real encoder
    auto& current_framebuffer = mFramebuffers[mCurrentFramebuffer];
"""

CPP_IMGUI_OLD = """void GfxRenderingAPIMetal::RenderDrawData(ImDrawData* drawData) {
    auto framebuffer = mFramebuffers[0];
"""
CPP_IMGUI_NEW = """void GfxRenderingAPIMetal::RenderDrawData(ImDrawData* drawData) {
    PbEnsureEncoder(0); // overlay 0033 - note the copy below, so this must come first
    auto framebuffer = mFramebuffers[0];
"""

# --- the sites that end or replace an encoder ---------------------------------
CPP_CLEAR_OLD = """    auto& framebuffer = mFramebuffers[mCurrentFramebuffer];

    // End the current render encoder
    framebuffer.mCommandEncoder->endEncoding();
"""
CPP_CLEAR_NEW = """    auto& framebuffer = mFramebuffers[mCurrentFramebuffer];

    // End the current render encoder. Overlay 0033: unless one was only OWED, in
    // which case there is nothing live to end and the clear encoder below simply
    // becomes the encoder that was owed.
    if (framebuffer.mPbRestartPending) {
        framebuffer.mPbRestartPending = false;
        framebuffer.mPbRestartLoads = false;
        framebuffer.mHasEndedEncoding = false;
    } else {
        framebuffer.mCommandEncoder->endEncoding();
    }
"""

CPP_COPY_END_OLD = """    // End the current render encoder
    source_framebuffer.mCommandEncoder->endEncoding();

    // Create a blit encoder
    MTL::BlitCommandEncoder* blit_encoder = source_framebuffer.mCommandBuffer->blitCommandEncoder();
    blit_encoder->setLabel(NS::String::string("Copy Framebuffer Encoder", NS::UTF8StringEncoding));
"""
CPP_COPY_END_NEW = """#ifdef __IOS__
    // Overlay 0012 rev 4's ledger: how many full-screen copies a frame really
    // emits, and over how many pixels. PM64 emits at least one unconditionally
    // (port_emitPrevFrameCapture), and at 2x supersampling that is 13.8 Mpx read
    // AND written - the (ii) suspect of the design notes D28 §2, priced on the line.
    gPBIosCopyN.fetch_add(1);
    gPBIosCopyPx.fetch_add((uint64_t)std::max(0, srcX1 - srcX0) * (uint64_t)std::max(0, srcY1 - srcY0));
#endif

    // End the current render encoder (overlay 0033: unless it was only owed).
    if (source_framebuffer.mPbRestartPending) {
        source_framebuffer.mPbRestartPending = false;
        source_framebuffer.mPbRestartLoads = false;
    } else {
        source_framebuffer.mCommandEncoder->endEncoding();
    }

    // Create a blit encoder
    MTL::BlitCommandEncoder* blit_encoder = source_framebuffer.mCommandBuffer->blitCommandEncoder();
    blit_encoder->setLabel(NS::String::string("Copy Framebuffer Encoder", NS::UTF8StringEncoding));
"""

CPP_COPY_RESTART_OLD = """    // Track the original load action and set the next load actions to Load to leverage the blit results
    MTL::RenderPassColorAttachmentDescriptor* srcColorAttachment =
        source_framebuffer.mRenderPassDescriptor->colorAttachments()->object(0);
    MTL::LoadAction origLoadAction = srcColorAttachment->loadAction();
    srcColorAttachment->setLoadAction(MTL::LoadActionLoad);

    MTL::RenderPassDepthAttachmentDescriptor* srcDepthAttachment =
        source_framebuffer.mRenderPassDescriptor->depthAttachment();
    MTL::LoadAction origDepthLoadAction = MTL::LoadActionDontCare;
    if (source_framebuffer.mHasDepthBuffer) {
        origDepthLoadAction = srcDepthAttachment->loadAction();
        srcDepthAttachment->setLoadAction(MTL::LoadActionLoad);
    }

    // Create a new render encoder back onto the framebuffer
    source_framebuffer.mCommandEncoder =
        source_framebuffer.mCommandBuffer->renderCommandEncoder(source_framebuffer.mRenderPassDescriptor);

    std::string fbce_label = fmt::format("FrameBuffer {} Command Encoder After Copy", fb_src_id);
    source_framebuffer.mCommandEncoder->setLabel(NS::String::string(fbce_label.c_str(), NS::UTF8StringEncoding));
    source_framebuffer.mCommandEncoder->setDepthClipMode(MTL::DepthClipModeClamp);
    source_framebuffer.mCommandEncoder->setViewport(*source_framebuffer.mViewport);
    source_framebuffer.mCommandEncoder->setScissorRect(*source_framebuffer.mScissorRect);

    // Now that the command encoder is started, we set the original load actions back for the next frame's use
    srcColorAttachment->setLoadAction(origLoadAction);
    if (source_framebuffer.mHasDepthBuffer) {
        srcDepthAttachment->setLoadAction(origDepthLoadAction);
    }

    // Reset the framebuffer so the encoder is setup again when rendering triangles
"""
CPP_COPY_RESTART_NEW = """    // OVERLAY 0033: the restart is OWED, not taken. Re-opening a render encoder
    // here unconditionally is what made every PM64 frame end in a render pass
    // that draws nothing - and an empty pass still loads and stores the whole
    // colour and Depth32Float attachment (13.8 Mpx each at 2x supersampling).
    // PbEnsureEncoder opens it if anything actually draws next; the load actions
    // it will use are the descriptor's own, which are LoadActionLoad at rest -
    // exactly what the code below used to set and then restore.
    source_framebuffer.mPbRestartPending = true;
    source_framebuffer.mPbRestartLoads = true; // rev 2: content exists - LOAD it
    source_framebuffer.mHasEndedEncoding = true;

    // Reset the framebuffer so the encoder is setup again when rendering triangles
"""

CPP_SELECTFB_OLD = """    FramebufferMetal& src = mFramebuffers[fb_id];
    if (src.mCommandEncoder != nullptr && !src.mHasEndedEncoding) {
        src.mCommandEncoder->endEncoding();

        MTL::RenderPassColorAttachmentDescriptor* colorAttachment =
            src.mRenderPassDescriptor->colorAttachments()->object(0);
        MTL::LoadAction origColorLoad = colorAttachment->loadAction();
        colorAttachment->setLoadAction(MTL::LoadActionLoad);

        MTL::RenderPassDepthAttachmentDescriptor* depthAttachment = src.mRenderPassDescriptor->depthAttachment();
        MTL::LoadAction origDepthLoad = MTL::LoadActionDontCare;
        if (src.mHasDepthBuffer) {
            origDepthLoad = depthAttachment->loadAction();
            depthAttachment->setLoadAction(MTL::LoadActionLoad);
        }

        src.mCommandEncoder = src.mCommandBuffer->renderCommandEncoder(src.mRenderPassDescriptor);
        std::string label = fmt::format("FrameBuffer {} Command Encoder After SelectTextureFb", fb_id);
        src.mCommandEncoder->setLabel(NS::String::string(label.c_str(), NS::UTF8StringEncoding));
        src.mCommandEncoder->setDepthClipMode(MTL::DepthClipModeClamp);
        src.mCommandEncoder->setViewport(*src.mViewport);
        src.mCommandEncoder->setScissorRect(*src.mScissorRect);

        colorAttachment->setLoadAction(origColorLoad);
        if (src.mHasDepthBuffer) {
            depthAttachment->setLoadAction(origDepthLoad);
        }

        src.mHasBoundVertexShader = false;
"""
CPP_SELECTFB_NEW = """    FramebufferMetal& src = mFramebuffers[fb_id];
    if (src.mCommandEncoder != nullptr && !src.mHasEndedEncoding) {
        src.mCommandEncoder->endEncoding();

        // OVERLAY 0033: owe the restart instead of taking it. The final composite
        // of every PM64 frame samples the game framebuffer through here and then
        // draws nothing more into it, so the re-opened encoder was an empty
        // render pass over a 5472x2520 colour + depth attachment.
        src.mPbRestartPending = true;
        src.mPbRestartLoads = true; // rev 2: content exists - LOAD it
        src.mHasEndedEncoding = true;

        src.mHasBoundVertexShader = false;
"""

CPP_MSAA_OLD = """        auto& source_framebuffer = mFramebuffers[fb_id_source];
        source_framebuffer.mCommandEncoder->endEncoding();
        source_framebuffer.mHasEndedEncoding = true;
"""
CPP_MSAA_NEW = """        auto& source_framebuffer = mFramebuffers[fb_id_source];
        if (source_framebuffer.mPbRestartPending) { // overlay 0033
            source_framebuffer.mPbRestartPending = false;
            source_framebuffer.mPbRestartLoads = false;
        } else {
            source_framebuffer.mCommandEncoder->endEncoding();
        }
        source_framebuffer.mHasEndedEncoding = true;
"""

CPP_MSAA2_OLD = """        // End the current render encoder
        auto& target_framebuffer = mFramebuffers[fb_id_target];
        target_framebuffer.mCommandEncoder->endEncoding();
"""
CPP_MSAA2_NEW = """        // End the current render encoder
        auto& target_framebuffer = mFramebuffers[fb_id_target];
        if (target_framebuffer.mPbRestartPending) { // overlay 0033
            target_framebuffer.mPbRestartPending = false;
            target_framebuffer.mPbRestartLoads = false;
        } else {
            target_framebuffer.mCommandEncoder->endEncoding();
        }
"""

CPP_READBACK_OLD = """        // Reset state so StartDrawToFramebuffer can create a fresh command buffer.
        // Example case: the FB can be reused for another sprite in the same frame.
        framebuffer.mCommandBuffer = nullptr;
        framebuffer.mCommandEncoder = nullptr;
        framebuffer.mHasEndedEncoding = false;
"""
CPP_READBACK_NEW = """        // Reset state so StartDrawToFramebuffer can create a fresh command buffer.
        // Example case: the FB can be reused for another sprite in the same frame.
        framebuffer.mCommandBuffer = nullptr;
        framebuffer.mCommandEncoder = nullptr;
        framebuffer.mHasEndedEncoding = false;
        framebuffer.mPbRestartPending = false; // overlay 0033 - the buffer is gone
        framebuffer.mPbRestartLoads = false;
"""

CPP_ENDFRAME_OLD = """    // Cleanup states
    for (int fb_id = 0; fb_id < (int)mFramebuffers.size(); fb_id++) {
        FramebufferMetal& fb = mFramebuffers[fb_id];

        fb.mLastShaderProgram = nullptr;
"""
CPP_ENDFRAME_NEW = """    // Cleanup states
    for (int fb_id = 0; fb_id < (int)mFramebuffers.size(); fb_id++) {
        FramebufferMetal& fb = mFramebuffers[fb_id];

        // OVERLAY 0033: a restart still owed at the end of the frame is a render
        // pass that was never encoded because nothing would have drawn into it.
        // That is the saving, so count it (perf line `pass_skipped=`, overlay
        // 0012 rev 4) rather than letting it be invisible.
        if (fb.mPbRestartPending) {
            fb.mPbRestartPending = false;
            fb.mPbRestartLoads = false;
#ifdef __IOS__
            gPBIosPassSkippedN.fetch_add(1);
#endif
        }

        fb.mLastShaderProgram = nullptr;
"""

CPP_POOL_OLD = """    mFrameAutoreleasePool->release();
}

void GfxRenderingAPIMetal::FinishRender() {
"""
CPP_POOL_NEW = """    mFrameAutoreleasePool->release();

    // OVERLAY 0033 REV 3: the deferred encoders' lifetime. PbEnsureEncoder
    // retains what renderCommandEncoder() hands it, because the pool that would
    // otherwise own it may be a LOCAL one (DrawTriangles opens its own for the
    // duration of one draw) and a render encoder released while still open is a
    // Metal assertion and an abort - M-032 §2, the 0.0.0.14 device crash. Every
    // one of them has had endEncoding() called above, so releasing them here,
    // once the frame pool is gone, is the exact lifetime upstream's eager
    // encoders had. Nothing else releases them.
    for (MTL::RenderCommandEncoder* pbEnc : mPbRetainedEncoders) {
        pbEnc->release();
    }
    mPbRetainedEncoders.clear();
}

void GfxRenderingAPIMetal::FinishRender() {
"""

EDITS.append((f"{LUS}/src/fast/backends/gfx_metal.cpp",
              [(CPP_DEF_OLD, CPP_DEF_NEW, 1),
               (CPP_POOL_OLD, CPP_POOL_NEW, 1),
               (CPP_STARTDRAW_OLD, CPP_STARTDRAW_NEW, 1),
               (CPP_VIEWPORT_OLD, CPP_VIEWPORT_NEW, 1),
               (CPP_SCISSOR_OLD, CPP_SCISSOR_NEW, 1),
               (CPP_DRAW_OLD, CPP_DRAW_NEW, 1),
               (CPP_IMGUI_OLD, CPP_IMGUI_NEW, 1),
               (CPP_CLEAR_OLD, CPP_CLEAR_NEW, 1),
               (CPP_COPY_END_OLD, CPP_COPY_END_NEW, 1),
               (CPP_COPY_RESTART_OLD, CPP_COPY_RESTART_NEW, 1),
               (CPP_SELECTFB_OLD, CPP_SELECTFB_NEW, 1),
               (CPP_MSAA_OLD, CPP_MSAA_NEW, 1),
               (CPP_MSAA2_OLD, CPP_MSAA2_NEW, 1),
               (CPP_READBACK_OLD, CPP_READBACK_NEW, 1),
               (CPP_ENDFRAME_OLD, CPP_ENDFRAME_NEW, 1)]))

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
