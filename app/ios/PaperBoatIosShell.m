// PaperBoatIosShell.m — UIScene lifecycle adoption for SDL2's UIKit backend.
//
// WHY THIS FILE EXISTS
// --------------------
// Built against the iOS 26+/27 SDK, UIKit traps any app that still runs the
// legacy UIApplicationDelegate-only lifecycle with no UIScene adoption:
//
//   EXC_BREAKPOINT (SIGTRAP)
//   0  UIKitCore  ___UIApplicationEvaluateRuntimeIssueForNoSceneLifecycleAdoption_block_invoke
//   …
//   23 UIKitCore  UIApplicationMain
//   24 Paperboat  SDL_UIKitRunApp
//
// SDL 2.32.10's UIKit backend has no UIScene code at all (`grep -r UIScene
// src/video/uikit/` is empty), so upstream PaperBoat's iOS target dies here
// before main() — see docs/upstream-ios-audit.md. A bare
// UIApplicationSceneManifest does NOT satisfy the check: UIKit wants real scene
// *adoption*, i.e. a UISceneConfigurations entry naming a scene delegate class.
//
// THE TWO HALVES
// --------------
// 1. ios/plist.in declares UISceneConfigurations →
//    UIWindowSceneSessionRoleApplication → { UISceneDelegateClassName =
//    PBIosSceneDelegate }. That is what clears the trap.
// 2. Once the app is scene-based, a UIWindow created the old way
//    (`[[UIWindow alloc] initWithFrame:]`, which is exactly what
//    SDL_uikitwindow.m's UIKit_CreateWindow does) has windowScene == nil and
//    will never appear on screen. So we graft SDL's window onto the connected
//    scene. The graft is hung off a swizzle of -[UIWindow makeKeyAndVisible]
//    (UIKit_ShowWindow's call) rather than off an LUS hook, which keeps the
//    vendor edit down to CMakeLists.txt + ios/plist.in.
//
// Ordering is safe: SDL's app delegate schedules postFinishLaunch (which runs
// SDL_main, and therefore SDL_CreateWindow) with performSelector:afterDelay:0,
// so the scene is connected before the game's window is ever created.
//
// Everything here is iOS-only and compiled into the Paperboat app target with
// ARC (-fobjc-arc, set by the CMake overlay).

// ROUND 4 ADDITIONS (the program's class (a) baseline)
// ---------------------------------------------------
//   * a launch-gated TCP console bridge on :8771 (compiled out of release
//     builds; docs/console-bridge.md),
//   * crash capture to Documents/crash.txt — LUS installs NO signal handler on
//     Apple platforms, so without this an iOS crash leaves nothing at all,
//   * a synchronous settings flush on resign-active/background, because an iOS
//     swipe-kill is SIGKILL and upstream has no background handling anywhere,
//   * paperboat:// deep links, queued at any lifecycle point and drained when
//     the engine is up.
// See the shell divergence notes for how this differs from the SoH port's shell.

#import <UIKit/UIKit.h>
#import <objc/runtime.h>
// Round 11 (overlay 0019): the SSAA device tier is keyed on the Metal GPU
// family, and the native drawable size comes off the CAMetalLayer SDL created.
// Both frameworks are already on this target's link line (libultraship links
// Metal and QuartzCore, and a static library propagates its PRIVATE link
// dependencies to the final link).
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>

#include <errno.h>
#include <execinfo.h>
#include <fcntl.h>
#include <limits.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <mach-o/dyld.h>
#include <sys/socket.h>
#include <sys/ucontext.h>
#include <time.h>
#include <unistd.h>

// The C++ side of the shell (app/ios/PaperBoatIosConsole.cpp): the only code in
// the port's shell that speaks libultraship.
extern int PBIos_EngineReady(void);
extern int PBIos_ConsoleRun(const char* line, char* out, int cap);
extern int PBIos_ConfigSave(void);
// CVar reads (round 11). The shell owns the defaults for the gSohIos.* block
// that Settings > iOS edits, so it has to be able to read those CVars back.
extern float PBIos_CVarGetFloat(const char* name, float defaultValue);
extern int PBIos_CVarGetInt(const char* name, int defaultValue);
// Effective render dimensions, published by overlay 0019 for the `ssaa` verb.
extern volatile uint32_t gPBIosSsaaW;
extern volatile uint32_t gPBIosSsaaH;

// The Phase 0.3 measurement harness (overlay 0012, src/port/Engine.cpp).
// Formats one PB_PERF line into `out`; returns its length, or 0 if the probe
// has nothing. Main-thread call, like every other bridge command.
extern int PBIos_PerfReport(char* out, int cap);

// The interpolation-target state (overlay 0023 rev 3): the refresh rate the
// engine is using, the target it settled on, and whether the auto-step-down has
// fired. Same main-thread rule as PBIos_PerfReport.
extern int PBIos_RefreshReport(char* out, int cap);

// ROUND 22 — the texture cache's ledger (overlay 0012 rev 7 / overlay 0037) and
// the physical-controller harness (app/ios/PaperBoatIosConsole.cpp, D33).
// PBIos_PadConnectedCount is compiled into every build; the injection verbs
// exist only when the bridge does.
extern int PBIos_TexCacheReport(char* out, int cap);
extern int PBIos_PadConnectedCount(void);
#if PAPERBOAT_REMOTE_CONSOLE
extern int PBIos_PadAttach(void);
extern int PBIos_PadDetach(void);
extern int PBIos_PadButton(const char* name, int down);
extern int PBIos_PadStick(const char* which, float x, float y);
extern int PBIos_PadReport(char* out, int cap);
#endif

// Forward declarations for the sections below (the scene delegate is defined
// before them and calls into all three).
static void PBIos_FlushConfig(const char* why);
static void PBIos_SetBackgrounded(int backgrounded);
static void PBIos_QueueDeepLink(NSURL* url);
static void PBIos_StartConsoleBridge(BOOL force);
// Round 13: the safe-area insets are cached; this invalidates them.
void PBIos_MarkSafeAreaDirty(void);
static void PBIos_WatchRemoteConsoleCVar(void);
static void PBIos_ConsumePendingIntent(void);
// The program's unified touch layer (round 12) — defined near the bottom of
// this file, installed from the two lifecycle points below.
static void PBIos_InstallTouchOverlay(void);
static NSString* PBIos_TouchProbe(NSArray<NSString*>* args);

// The scene we hand SDL's window to. Strong is wrong (UIKit owns the scene and
// a stale one after a disconnect would be a dangling graft target); weak lets
// it go and PBIos_ActiveWindowScene() re-finds the live one.
static __weak UIWindowScene* gPBScene = nil;

// Prefer the scene the delegate was connected to; fall back to whatever
// UIWindowScene the app currently has (foregrounded first). The fallback
// matters when the graft runs before the delegate callback for any reason.
static UIWindowScene* PBIos_ActiveWindowScene(void) {
    UIWindowScene* s = gPBScene;
    if (s != nil && s.activationState != UISceneActivationStateUnattached) {
        return s;
    }
    UIWindowScene* any = nil;
    for (UIScene* scene in UIApplication.sharedApplication.connectedScenes) {
        if (![scene isKindOfClass:UIWindowScene.class]) {
            continue;
        }
        UIWindowScene* ws = (UIWindowScene*)scene;
        if (ws.activationState == UISceneActivationStateForegroundActive) {
            return ws;
        }
        if (any == nil) {
            any = ws;
        }
    }
    return any;
}

// Put one scene-less window onto the scene and size it to the scene's own
// coordinate space. SDL sized the window from UIScreen.bounds, which is
// portrait-shaped even for a landscape-only app; the scene's bounds are the
// truth the renderer must agree with.
static void PBIos_AdoptWindow(UIWindow* w) {
    if (w == nil || w.windowScene != nil) {
        return;
    }
    UIWindowScene* scene = PBIos_ActiveWindowScene();
    if (scene == nil) {
        NSLog(@"[PaperBoatIosShell] WARNING: no UIWindowScene to adopt window %@ into", w);
        return;
    }
    CGRect before = w.frame;
    w.windowScene = scene;
    CGRect sb = scene.coordinateSpace.bounds;
    if (sb.size.width > 1 && sb.size.height > 1 && !CGRectEqualToRect(w.frame, sb)) {
        w.frame = sb;
    }
    // The spec rule 6: a UIKit placement claim needs the view logging its own
    // frame — engine screenshots cannot see UIKit.
    NSLog(@"[PaperBoatIosShell] adopted window %@ into scene %@: frame %.0fx%.0f -> %.0fx%.0f "
          @"(scene %.0fx%.0f, scale %.1f)",
          w, scene, before.size.width, before.size.height,
          w.frame.size.width, w.frame.size.height, sb.size.width, sb.size.height,
          (double)scene.screen.nativeScale);
}

// Deliberately walks connectedScenes rather than UIApplication.windows: that
// property is deprecated from iOS 15 and, in a scene world, does not
// reliably list a window that has no scene yet — which is precisely the
// window we are looking for. This is the belt to the swizzle's braces; the
// swizzle below is the path that actually catches SDL's window.
static void PBIos_AdoptAllWindows(void) {
    for (UIScene* scene in UIApplication.sharedApplication.connectedScenes) {
        if (![scene isKindOfClass:UIWindowScene.class]) {
            continue;
        }
        for (UIWindow* w in ((UIWindowScene*)scene).windows) {
            PBIos_AdoptWindow(w);
        }
    }
}

// ---------------------------------------------------------------------------
// The scene delegate named by ios/plist.in. Deliberately minimal: it does not
// create a window (SDL owns that) — it exists so UIKit sees scene adoption and
// so we learn which scene to graft onto.
// ---------------------------------------------------------------------------
@interface PBIosSceneDelegate : NSObject <UIWindowSceneDelegate>
@end

@implementation PBIosSceneDelegate

- (void)scene:(UIScene*)scene
    willConnectToSession:(UISceneSession*)session
                 options:(UISceneConnectionOptions*)connectionOptions {
    if ([scene isKindOfClass:UIWindowScene.class]) {
        gPBScene = (UIWindowScene*)scene;
        NSLog(@"[PaperBoatIosShell] scene connected: %@", scene);
        // Nothing of SDL's exists yet at this point on a cold launch; harmless
        // then, and the thing that fixes a scene RECONNECT (a new scene object
        // is handed over and the old graft is stale, which shows as a black
        // screen with the game still running).
        PBIos_AdoptAllWindows();
    }
    // A cold launch through a link delivers the URL HERE, not through
    // scene:openURLContexts: — that one only fires while the app is already
    // running. Miss this and `simctl openurl` on a dead app silently just
    // launches it.
    for (UIOpenURLContext* ctx in connectionOptions.URLContexts) {
        PBIos_QueueDeepLink(ctx.URL);
    }
}

- (void)sceneDidBecomeActive:(UIScene*)scene {
    if ([scene isKindOfClass:UIWindowScene.class]) {
        gPBScene = (UIWindowScene*)scene;
    }
    PBIos_AdoptAllWindows();
    PBIos_MarkSafeAreaDirty(); // new scene / new geometry
    // Idempotent: a scene reconnect hands over a new window and the overlay
    // went with the old one.
    PBIos_InstallTouchOverlay();
    PBIos_SetBackgrounded(0);
    // An App Intent run while the app is already alive drops its request in
    // UserDefaults and foregrounds us; this is where that arrives.
    PBIos_ConsumePendingIntent();
}

// The app is running: UIKit hands a tapped/`simctl openurl` link over here.
// Under a scene session the legacy application:openURL: never fires at all
// (SoH port finding, re-confirmed here: SDL's app delegate has no URL code
// either, so this delegate is the ONLY delivery path).
- (void)scene:(UIScene*)scene openURLContexts:(NSSet<UIOpenURLContext*>*)URLContexts {
    for (UIOpenURLContext* ctx in URLContexts) {
        PBIos_QueueDeepLink(ctx.URL);
    }
}

// iOS swipe-kill is SIGKILL: nothing runs at exit, so the LAST chance to write
// settings is the moment the app stops being frontmost. Upstream has no
// SDL_APP_WILLENTERBACKGROUND / DIDENTERBACKGROUND / TERMINATING handling at
// all (docs/upstream-ios-audit.md), so without these four methods every setting
// changed since launch is lost on the swipe that follows.
- (void)sceneWillResignActive:(UIScene*)scene {
    PBIos_FlushConfig("sceneWillResignActive");
}

- (void)sceneDidEnterBackground:(UIScene*)scene {
    PBIos_FlushConfig("sceneDidEnterBackground");
    PBIos_SetBackgrounded(1);
}

- (void)sceneWillEnterForeground:(UIScene*)scene {
    PBIos_SetBackgrounded(0);
}

@end

// ---------------------------------------------------------------------------
// Build stamp, at launch.
//
// The program's rule is that the build stamp is generated source compiled into
// the binary, asserted post-build AND at launch. gPortBuildStamp comes from
// vendor/PaperBoat/src/port/build.c.in (overlay 0005), configured from
// PAPERBOAT_IOS_VERSION / PAPERBOAT_IOS_BUILD; the Info.plist values beside it
// come from the same two CMake variables by a different route, so printing both
// on one line is also a check that the two routes agree. NSLog, not spdlog: this
// runs at +load time, long before libultraship has a logger.
// ---------------------------------------------------------------------------
extern const char gPortBuildStamp[];

static void PBIos_LogBuildStamp(void) {
    NSDictionary* info = NSBundle.mainBundle.infoDictionary;
    NSLog(@"[PaperBoatIosShell] build stamp: %s", gPortBuildStamp);
    NSLog(@"[PaperBoatIosShell] bundle: %@ version %@ (build %@)",
          info[@"CFBundleIdentifier"], info[@"CFBundleShortVersionString"],
          info[@"CFBundleVersion"]);
}

// ---------------------------------------------------------------------------
// Backgrounding, and the settings flush (round 4).
//
// Two separate problems share one lifecycle hook:
//
//  1. SETTINGS. iOS kills a swiped-away app with SIGKILL. Upstream writes
//     paperboat.cfg.json only when something changes a CVar (and, before
//     overlay 0004, not even then), and has no background/terminate handling
//     anywhere, so the flush has to happen while the app is still alive —
//     synchronously, on the way out.
//  2. RENDERING. Metal's nextDrawable must not be called while the process is
//     backgrounded: it stalls, and background GPU work risks a watchdog kill.
//     Upstream's LUS already has the right gate — Fast3dWindow skips the whole
//     render when GfxWindowBackendSDL2::IsWindowVisible() is false — but that
//     function only looks at SDL's MINIMIZED/HIDDEN flags, which SDL 2's UIKit
//     backend never sets for a scene-based app. Overlay 0008 teaches it to ask
//     this flag as well, which is why PBIos_IsBackgrounded has C linkage and a
//     stable name.
// ---------------------------------------------------------------------------

// One writer (main thread), one reader (the game loop, also the main thread on
// iOS, plus whatever thread LUS asks from). volatile is the right weight: a
// reader tolerating a frame of staleness is exactly the contract.
static volatile int gPBBackgrounded = 0;

int PBIos_IsBackgrounded(void) {
    return gPBBackgrounded;
}

// The OS thermal state, 0..3 (nominal / fair / serious / critical). Exported
// with C linkage because the perf harness inside the engine (overlay 0012)
// puts it in every PB_PERF line: the program's measurement protocol treats a
// serious or critical reading as invalidating the comparison, so the number
// has to travel WITH the frame times, not beside them in someone's notes.
// NSProcessInfo caches this; it is not an expensive call.
int PBIos_ThermalState(void) {
    return (int)NSProcessInfo.processInfo.thermalState;
}

static void PBIos_SetBackgrounded(int backgrounded) {
    if (gPBBackgrounded != backgrounded) {
        NSLog(@"[PaperBoatIosShell] backgrounded=%d", backgrounded);
    }
    gPBBackgrounded = backgrounded;
}

// ---------------------------------------------------------------------------
// Safe-area insets, published to the engine (round 6, overlay 0013).
//
// Upstream draws its touch pad IN THE ENGINE (src/port/ui/TouchControls.cpp),
// laid out against ImGui's DisplaySize, and puts its corner elements hard in
// the corners. On a real iPhone those corners are not usable screen: in
// landscape the sensor housing and the home indicator carve ~68 pt off each
// side and ~20 pt off the bottom (measured, iPhone Air / iOS 27 — below).
// Nothing in the engine can see that; UIKit is the only thing that knows.
//
// WHERE THE NUMBERS COME FROM, AND WHY NOT FROM SDL'S WINDOW
// -----------------------------------------------------------
// The obvious source — SDL's own UIWindow, the one the scene graft adopts —
// is WRONG on this port, and wrong in a way that looks plausible. Measured on
// the simulator with the scene in landscape (interfaceOrientation = 3,
// screen.bounds = 912x420, window frame = 912x420):
//
//     SDL_uikitwindow        safeAreaInsets = (t 68, l 0, b 34, r 0)
//     a window UIKit made    safeAreaInsets = (t 0, l 68, b 20, r 68)
//
// SDL's window reports the PORTRAIT inset set on a landscape window, and a
// forced -layoutIfNeeded does not shake it loose: SDL creates that window
// from UIScreen.bounds before any scene exists (which is why this port needs
// the graft in overlay 0001 at all), and UIKit never re-resolves its safe area
// afterwards. Taking those numbers at face value would push the pad off the
// TOP and BOTTOM — neither of which is occluded in landscape — and leave the
// two edges that actually are occluded untouched. Rotating them by the
// interface orientation is no better: the magnitudes differ per orientation
// (portrait bottom 34 vs landscape bottom 20), so the result is wrong by 14 pt
// on the edge that matters most.
//
// So the shell keeps a PROBE WINDOW: an empty, non-interactive, effectively
// invisible UIWindow created against the scene by us, which UIKit therefore
// lays out itself and gives the correct insets for the current orientation.
// It is the cheapest honest source — no private API, no orientation algebra,
// and it follows rotation for free. If there is no scene yet, the insets are
// zero, which is exactly upstream's behaviour.
//
// UNITS. **The insets are published in DRAWABLE PIXELS, not points.** This LUS
// fork runs ImGui and all window geometry in drawable pixels on iOS
// (io.DisplaySize from SDL_GetRendererOutputSize, DisplayFramebufferScale
// (1,1) — docs/lus-divergence.md "ImGui runs in drawable-pixel space"), and
// TouchControls.cpp lays out against exactly that space, so a value in points
// would be wrong by the screen scale (3x here: a 68 pt inset applied as 68 px
// moves a button a third of the way it needed to go, which reads as "the fix
// did nothing" rather than as a unit bug).
//
// FRESHNESS. UIWindow.safeAreaInsets is only meaningful on the main thread and
// changes on rotation and scene reconnect. Rather than hunt every notification
// that could move it, the getter recomputes whenever it is called on the main
// thread — which is every frame, because the game loop IS the main thread on
// iOS — and serves the last known value to any other caller. The spec rule 6 (a
// UIKit placement claim needs the view logging its own frame): every CHANGE is
// logged with the probe's frame, the scale, the points and the pixels handed
// over.
// ---------------------------------------------------------------------------

static float gPBSafeTop = 0.0f, gPBSafeLeft = 0.0f, gPBSafeBottom = 0.0f, gPBSafeRight = 0.0f;
static UIWindow* gPBSafeProbe = nil;

// The probe window described above. Strong, and rebuilt if the scene changes:
// a probe left over from a disconnected scene reports that scene's geometry.
static UIWindow* PBIos_SafeAreaProbe(void) {
    UIWindowScene* scene = PBIos_ActiveWindowScene();
    if (scene == nil) {
        return nil;
    }
    if (gPBSafeProbe != nil && gPBSafeProbe.windowScene != scene) {
        gPBSafeProbe.hidden = YES;
        gPBSafeProbe = nil;
    }
    if (gPBSafeProbe == nil) {
        UIWindow* w = [[UIWindow alloc] initWithWindowScene:scene];
        w.rootViewController = [[UIViewController alloc] init];
        w.backgroundColor = UIColor.clearColor;
        w.userInteractionEnabled = NO;
        // Below the game's window and never made key: it must not take input,
        // steal key status, or appear over anything. alpha 0 (rather than
        // hidden) would be the tidier "invisible", but a hidden window is not
        // laid out and would report nothing.
        w.windowLevel = UIWindowLevelNormal - 100;
        w.alpha = 0.01;
        w.hidden = NO;
        gPBSafeProbe = w;
        NSLog(@"[PaperBoatIosShell] safe-area probe window created on scene %@", scene);
    }
    CGRect b = scene.coordinateSpace.bounds;
    if (b.size.width > 1 && b.size.height > 1 && !CGRectEqualToRect(gPBSafeProbe.frame, b)) {
        gPBSafeProbe.frame = b; // follow rotation / scene resize
    }
    return gPBSafeProbe;
}

// Round 13 (the ~5 s stutter): the insets are CACHED, and this is the only
// thing that recomputes them.
//
// PBIos_GetSafeAreaInsets is called three times per rendered frame — the touch
// layer, the menu and PaperboatGui each ask (overlay 0013/0017) — and it used
// to run `[w layoutIfNeeded]` on the probe window every single time. That is a
// synchronous UIKit layout pass on the game loop's thread, ~90 times a second,
// to produce four numbers that change only on rotation or a scene resize.
//
// So: recompute when something says it changed (PBIos_MarkSafeAreaDirty, from
// scene activation and the overlay's own layoutSubviews), and otherwise at most
// once a second as a backstop in case a notification is ever missed. The
// `safearea` bridge verb marks dirty first, so it always reports live values —
// that is the check the rotation test uses.
static BOOL gPBSafeDirty = YES;
static NSTimeInterval gPBSafeStamp = 0.0;

void PBIos_MarkSafeAreaDirty(void) {
    gPBSafeDirty = YES;
}

static void PBIos_RefreshSafeAreaInsets(void) {
    UIWindow* w = PBIos_SafeAreaProbe();
    if (w == nil) {
        return; // no scene yet: keep zeros, which is upstream's own behaviour
    }
    [w layoutIfNeeded];
    UIEdgeInsets pts = w.safeAreaInsets;
    const CGFloat scale = w.windowScene != nil ? w.windowScene.screen.nativeScale : UIScreen.mainScreen.nativeScale;
    const float top = (float)(pts.top * scale);
    const float left = (float)(pts.left * scale);
    const float bottom = (float)(pts.bottom * scale);
    const float right = (float)(pts.right * scale);
    if (top != gPBSafeTop || left != gPBSafeLeft || bottom != gPBSafeBottom || right != gPBSafeRight) {
        NSLog(@"[PaperBoatIosShell] safe-area insets: probe frame %.0fx%.0f pt, scale %.1f; "
              @"points t=%.1f l=%.1f b=%.1f r=%.1f -> pixels t=%.0f l=%.0f b=%.0f r=%.0f",
              w.frame.size.width, w.frame.size.height, (double)scale,
              (double)pts.top, (double)pts.left, (double)pts.bottom, (double)pts.right,
              (double)top, (double)left, (double)bottom, (double)right);
    }
    gPBSafeTop = top;
    gPBSafeLeft = left;
    gPBSafeBottom = bottom;
    gPBSafeRight = right;
}

// The engine's entry point (overlay 0013 declares this in TouchControls.cpp).
// Any of the four pointers may be NULL.
void PBIos_GetSafeAreaInsets(float* top, float* left, float* bottom, float* right) {
    if (NSThread.isMainThread) {
        const NSTimeInterval now = CACurrentMediaTime();
        if (gPBSafeDirty || now - gPBSafeStamp >= 1.0) {
            gPBSafeDirty = NO;
            gPBSafeStamp = now;
            PBIos_RefreshSafeAreaInsets();
        }
    }
    if (top != NULL) {
        *top = gPBSafeTop;
    }
    if (left != NULL) {
        *left = gPBSafeLeft;
    }
    if (bottom != NULL) {
        *bottom = gPBSafeBottom;
    }
    if (right != NULL) {
        *right = gPBSafeRight;
    }
}

// ---------------------------------------------------------------------------
// SSAA — the two hooks overlay 0019 calls out of Interpreter::StartFrame.
//
//   float    PBIos_SsaaFactor(void)         gSohIos.Supersample, or the tier
//   uint32_t PBIos_DrawablePixelWidth(void) the CAMetalLayer's px long edge
//
// WHY THE DEFAULT IS A DEVICE TIER AND NOT A NUMBER. Unset means "pick for me",
// so a capable phone supersamples without the player hunting for a slider.
// Keyed on the Metal GPU FAMILY rather than a model table, so new hardware
// inherits sensibly: Apple8 (A16 / M2 and later) -> 2.0, Apple7 (A14/A15) ->
// 1.5, anything older -> 1.0. The slider always wins once it has a value.
// Clamped to [1.0, 2.0] either way: below 1.0 would re-create the sub-native
// upscale 0019 exists to remove, and above 2.0 is GPU spend with nothing on
// screen to show for it.
//
// THREADING. Both are called from the game loop, which on iOS is the main
// thread, every frame. The drawable size is refreshed on the main thread and
// cached, exactly like the safe-area insets above; an off-thread caller reads
// the cache. Zero means "not sized yet", and 0019 treats that as "do nothing",
// which is byte-identical to upstream.
// ---------------------------------------------------------------------------

static float gPBDrawableW = 0.0f, gPBDrawableH = 0.0f;

static CAMetalLayer* PBIos_FindMetalLayer(UIView* view) {
    if ([view.layer isKindOfClass:CAMetalLayer.class]) {
        return (CAMetalLayer*)view.layer;
    }
    for (UIView* sub in view.subviews) {
        CAMetalLayer* found = PBIos_FindMetalLayer(sub);
        if (found != nil) {
            return found;
        }
    }
    return nil;
}

static void PBIos_RefreshDrawableSize(void) {
    float w = 0.0f, h = 0.0f;
    UIWindowScene* scene = PBIos_ActiveWindowScene();
    for (UIWindow* win in scene.windows) {
        if (win == gPBSafeProbe) {
            continue; // our own empty probe window has no Metal layer anyway
        }
        CAMetalLayer* layer = PBIos_FindMetalLayer(win);
        if (layer != nil && layer.drawableSize.width > 1 && layer.drawableSize.height > 1) {
            w = (float)layer.drawableSize.width;
            h = (float)layer.drawableSize.height;
            break;
        }
    }
    if (w < 1.0f || h < 1.0f) {
        // Fallback: the panel's own native pixel size. Correct for this port
        // (the game window is full-screen and landscape-locked) and it is the
        // only answer available before SDL has created the Metal view at all.
        const CGRect nb = UIScreen.mainScreen.nativeBounds;
        w = (float)nb.size.width;
        h = (float)nb.size.height;
    }
    if (w != gPBDrawableW || h != gPBDrawableH) {
        NSLog(@"[PaperBoatIosShell] drawable size: %.0fx%.0f px", (double)w, (double)h);
    }
    gPBDrawableW = w;
    gPBDrawableH = h;
}

uint32_t PBIos_DrawablePixelWidth(void) {
    if (NSThread.isMainThread) {
        PBIos_RefreshDrawableSize();
    }
    const float w = gPBDrawableW, h = gPBDrawableH;
    return (uint32_t)(w > h ? w : h); // long edge — this port is landscape-locked
}

// The drawable-pixels-per-point ratio, for overlay 0022.
//
// WHY THE ENGINE NEEDS IT AT ALL. Every sibling port runs ImGui in POINT space
// (DisplaySize 912x420, DisplayFramebufferScale 3x — Lighthouse's overlay 0010
// exists to make that true), so the family's menu scale of 0.85 lands at a
// readable physical size. THIS fork runs ImGui in DRAWABLE PIXELS with
// DisplayFramebufferScale (1,1) (Fast3dGui::ImGuiWMNewFrame's __IOS__ branch;
// the design notes D16), so the same 0.85 renders every glyph, every button and every
// modal at ONE THIRD of the size it has on the siblings. That is the "the
// 'no o2r files' window is way too small" the user reported on 0.0.0.5.
//
// So the engine multiplies the family's scale by this ratio and lands on the
// siblings' apparent size. Cached and constant for the app's life: this port
// is landscape-locked to one screen, and ScaleImGui is called from the game
// loop (including once from GameEngine's constructor, before the first-run
// modal is ever drawn, which is the whole point).
float PBIos_DisplayScale(void) {
    static float cached = 0.0f;
    if (cached > 0.0f) {
        return cached;
    }
    // nativeScale, not the CAMetalLayer ratio: it is correct from the very
    // first frame, before SDL has created the Metal view, and on this port the
    // two agree exactly (iPhone Air: 912x420 pt window, 2736x1260 px drawable).
    const CGFloat s = UIScreen.mainScreen.nativeScale;
    cached = (s >= 1.0 && s <= 4.0) ? (float)s : 1.0f;
    NSLog(@"[PaperBoatIosShell] ImGui display scale %.2f (drawable px per point)", (double)cached);
    return cached;
}

// The panel's own maximum refresh rate, in Hz (round 16, overlay 0023 rev 3).
//
// The engine used to take its refresh rate only from SDL's display mode, which
// on iOS is `UIScreen.maximumFramesPerSecond` anyway (SDL2's
// `UIKit_GetDisplayModeRefreshRate`) — but going to UIKit directly means the
// number does not depend on SDL having created a window yet, it is the value
// the rest of the ProMotion plumbing is keyed to, and the bridge can print it
// beside what the engine decided (the `refresh` verb). 60 when UIKit has
// nothing sane to say.
int PBIos_MaxFramesPerSecond(void) {
    UIWindowScene* scene = PBIos_ActiveWindowScene();
    NSInteger hz = scene != nil ? scene.screen.maximumFramesPerSecond : UIScreen.mainScreen.maximumFramesPerSecond;
    if (hz < 30 || hz > 240) {
        hz = 60;
    }
    return (int)hz;
}

static float PBIos_SsaaDeviceDefault(void) {
    static float cached = 0.0f;
    if (cached > 0.0f) {
        return cached;
    }
    cached = 1.0f;
    id<MTLDevice> dev = MTLCreateSystemDefaultDevice();
    if (dev != nil) {
        if ([dev supportsFamily:MTLGPUFamilyApple8]) {
            cached = 2.0f;
        } else if ([dev supportsFamily:MTLGPUFamilyApple7]) {
            cached = 1.5f;
        }
    }
    NSLog(@"[PaperBoatIosShell] SSAA device default %.2fx", (double)cached);
    return cached;
}

// The device tier, for the Settings > iOS > SSAA slider's DefaultValue (overlay
// 0021 rev 3). Round 15: before this the widget read a hardcoded 1.00x while
// the engine rendered at the tier, and the user had to drag the slider to 2.00 on
// a phone that was already at 2.00. UIWidgets reads a slider as
// CVarGetFloat(cvar, defaultValue), so handing it the tier makes the widget
// TELL THE TRUTH without seeding the CVar - the config still follows the phone
// it is opened on, which is why D23 rejected seeding in the first place.
float PBIos_SsaaDefaultValue(void) {
    return PBIos_SsaaDeviceDefault();
}

// The texture cache's byte budget, in MB, when the player has not set
// `gSohIos.TexCacheBudgetMB` (overlay 0037 reads this through a C ABI). A
// QUARTER of physical memory, capped at the SoH port's flat 1536 MB and floored
// at 192 MB. Reasoning in D33: the cap is what the SoH port tuned on the same
// class of phone, the fraction is what keeps a 4 GB device from handing a
// texture cache more than the whole app is likely to be allowed, and the floor
// keeps the cache larger than any single scene's working set. Logged once,
// because a budget nobody can see is a budget nobody can explain.
int PBIos_TexCacheBudgetDefaultMB(void) {
    static int cached = 0;
    if (cached > 0) {
        return cached;
    }
    const unsigned long long physMb = (unsigned long long)(NSProcessInfo.processInfo.physicalMemory >> 20);
    unsigned long long budget = physMb / 4;
    if (budget > 1536) {
        budget = 1536;
    }
    if (budget < 192) {
        budget = 192;
    }
    cached = (int)budget;
    NSLog(@"[PaperBoatIosShell] texture cache budget default %d MB (physical %llu MB)", cached, physMb);
    return cached;
}

float PBIos_SsaaFactor(void) {
    float f = PBIos_CVarGetFloat("gSohIos.Supersample", 0.0f);
    if (f < 1.0f) {
        f = PBIos_SsaaDeviceDefault(); // unset (or nonsense) means "pick for me"
    }
    if (f < 1.0f) {
        f = 1.0f;
    }
    if (f > 2.0f) {
        f = 2.0f;
    }
    return f;
}

static void PBIos_FlushConfig(const char* why) {
    if (!PBIos_EngineReady()) {
        return; // nothing to save yet: the config is still upstream's defaults
    }
    const int ok = PBIos_ConfigSave();
    NSLog(@"[PaperBoatIosShell] config flush (%s): %s", why ? why : "?", ok ? "written" : "no config yet");
}

// Belt to the scene delegate's braces. Under a scene session UIKit posts
// UISceneWillDeactivate rather than the app-level UIApplicationWillResignActive
// — but which one arrives has moved between iOS releases, and the cost of
// observing all four is one JSON write that is already idempotent.
static void PBIos_InstallLifecycleObservers(void) {
    void (^flush)(NSNotification*) = ^(NSNotification* note) {
        PBIos_FlushConfig(note.name.UTF8String);
    };
    NSNotificationCenter* nc = NSNotificationCenter.defaultCenter;
    [nc addObserverForName:UIApplicationWillResignActiveNotification object:nil queue:NSOperationQueue.mainQueue usingBlock:flush];
    [nc addObserverForName:UISceneWillDeactivateNotification object:nil queue:NSOperationQueue.mainQueue usingBlock:flush];
    [nc addObserverForName:UIApplicationDidEnterBackgroundNotification
                    object:nil
                     queue:NSOperationQueue.mainQueue
                usingBlock:^(NSNotification* note) {
                    PBIos_FlushConfig(note.name.UTF8String);
                    PBIos_SetBackgrounded(1);
                }];
    [nc addObserverForName:UISceneDidEnterBackgroundNotification
                    object:nil
                     queue:NSOperationQueue.mainQueue
                usingBlock:^(NSNotification* note) {
                    PBIos_FlushConfig(note.name.UTF8String);
                    PBIos_SetBackgrounded(1);
                }];
    // The clears matter more than the sets: a flag stuck at 1 is a permanently
    // black screen with the game still running, the single most expensive
    // symptom this shell can produce.
    [nc addObserverForName:UISceneWillEnterForegroundNotification
                    object:nil
                     queue:NSOperationQueue.mainQueue
                usingBlock:^(NSNotification* note) { PBIos_SetBackgrounded(0); }];
    [nc addObserverForName:UIApplicationWillEnterForegroundNotification
                    object:nil
                     queue:NSOperationQueue.mainQueue
                usingBlock:^(NSNotification* note) { PBIos_SetBackgrounded(0); }];
    [nc addObserverForName:UIApplicationDidBecomeActiveNotification
                    object:nil
                     queue:NSOperationQueue.mainQueue
                usingBlock:^(NSNotification* note) { PBIos_SetBackgrounded(0); }];
}

// ---------------------------------------------------------------------------
// Crash capture → Documents/crash.txt
//
// LUS's CrashHandler installs NOTHING on Apple platforms: its implementation is
// `#if defined(__linux__) && !defined(__ANDROID__)` (POSIX signals + ucontext
// register dumps) or `_WIN32` (SEH), and Context::InitCrashHandler on iOS
// therefore constructs an object that never handles a signal. The port's own
// GameEngine_LogStackTrace() does have an execinfo path, but it is only ever
// called deliberately and logs through spdlog — and a springboard-launched app
// has no terminal to log to. So an iOS crash today leaves: nothing.
//
// Four capture routes, each answering a different way of dying:
//   1. signals (SIGSEGV/SIGABRT/SIGBUS/SIGILL/SIGFPE/SIGTRAP/SIGSYS) on an
//      ALTERNATE STACK, so a stack-overflow SIGSEGV still has room to run;
//   2. uncaught Objective-C exceptions, which carry a name and a reason that a
//      bare SIGABRT backtrace does not;
//   3. std::terminate (an uncaught C++ throw reaches it before abort());
//   4. atexit, which catches an exit(0) — and this port calls exit(0) on a
//      missing pm64.o2r (Context.cpp's __IOS__ path), so "the app vanished" has
//      a benign explanation on record rather than looking like a crash.
//
// Installed from +load: earlier than main(), earlier than the scene, earlier
// than any engine code — which is what "early enough to catch a crash in scene
// setup" requires.
// ---------------------------------------------------------------------------

static char sPBCrashPath[1024];
static char sPBStamp[192];
static volatile int32_t sPBCrashWritten = 0;

static void PBIos_CrashPaths(void) {
    if (sPBCrashPath[0] != '\0') {
        return;
    }
    const char* home = getenv("HOME");
    snprintf(sPBCrashPath, sizeof(sPBCrashPath), "%s/Documents/crash.txt", home ? home : "/tmp");
    NSDictionary* info = NSBundle.mainBundle.infoDictionary;
    // Cached as a C string at ARM time: a signal handler must not go anywhere
    // near Foundation, and this is the one piece of Foundation state the record
    // cannot do without (the program rule: every artifact names its build).
    snprintf(sPBStamp, sizeof(sPBStamp), "%s | bundle %s %s (build %s)", gPortBuildStamp,
             [info[@"CFBundleIdentifier"] ?: @"?" UTF8String],
             [info[@"CFBundleShortVersionString"] ?: @"?" UTF8String],
             [info[@"CFBundleVersion"] ?: @"?" UTF8String]);
}

// Async-signal-safe *enough*: no malloc, no NSLog, no stdio streams. dprintf and
// strftime are not formally on the list, but they are what the shipped
// predecessor used and they are the only way to get readable output; a handler
// that is theoretically pure and practically empty is worth nothing.
//
// THE BACKTRACE, AND WHY IT IS NOT backtrace() (measured this round).
// The first version of this handler called backtrace()/backtrace_symbols_fd and
// produced a crash.txt with a perfectly good header and ZERO frames — twice,
// including after warming backtrace() up on a healthy stack. On arm64 the
// handler runs on the sigaltstack with no frame-pointer link back to the
// faulting stack, so the in-process unwinder stops before it starts. (Dropping
// SA_ONSTACK would fix that and give up catching stack-overflow SIGSEGV, which
// is one of the crashes most worth catching.)
//
// So a signal record walks the frame-pointer chain itself, starting from the
// registers in the ucontext the kernel handed the handler: pc, then lr, then
// [fp] / [fp+8] repeatedly. Each address is written as it is found rather than
// collected first — a wild fp faults again inside the handler, and when it does
// everything up to that point is already on disk. Non-signal records (an ObjC
// exception, std::terminate) run on a healthy stack, where backtrace() works.
static void PBIos_WriteFrameAddresses(int fd, void* uap) {
#if defined(__arm64__) || defined(__aarch64__)
    if (uap != NULL) {
        const ucontext_t* uc = (const ucontext_t*)uap;
        const uint64_t pc = (uint64_t)arm_thread_state64_get_pc(uc->uc_mcontext->__ss);
        const uint64_t lr = (uint64_t)arm_thread_state64_get_lr(uc->uc_mcontext->__ss);
        uint64_t fp = (uint64_t)arm_thread_state64_get_fp(uc->uc_mcontext->__ss);
        dprintf(fd, "0 %p (pc)\n", (void*)pc);
        dprintf(fd, "1 %p (lr)\n", (void*)lr);
        for (int i = 2; i < 64 && fp != 0 && (fp & 0x7) == 0; i++) {
            const uint64_t next = *(const uint64_t*)fp;
            const uint64_t ret = *(const uint64_t*)(fp + 8);
            if (ret == 0) {
                break;
            }
            dprintf(fd, "%d %p\n", i, (void*)ret);
            if (next <= fp) {
                break; // a chain that does not climb is a corrupt chain
            }
            fp = next;
        }
        return;
    }
#endif
    void* frames[96];
    const int n = backtrace(frames, 96);
    backtrace_symbols_fd(frames, n, fd);
}

static void PBIos_WriteCrashRecord(const char* kind, int sig, const char* note, void* uap) {
    PBIos_CrashPaths();
    if (__sync_lock_test_and_set(&sPBCrashWritten, 1) != 0) {
        return; // first writer wins — a terminate handler that then abort()s
                // must not overwrite its named reason with a bare SIGABRT
    }
    // APPEND, never truncate: the crash before this one is often the diagnosis,
    // and a user handing over crash.txt should be handing over the history.
    const int fd = open(sPBCrashPath, O_CREAT | O_WRONLY | O_APPEND, 0644);
    if (fd < 0) {
        return;
    }
    char when[32] = "?";
    const time_t now = time(NULL);
    struct tm utc;
    if (gmtime_r(&now, &utc) != NULL) {
        strftime(when, sizeof(when), "%Y-%m-%dT%H:%M:%SZ", &utc);
    }
    dprintf(fd, "\n=== PaperBoat-ios crash ===\n");
    dprintf(fd, "when=%s\n", when);
    dprintf(fd, "build=%s\n", sPBStamp);
    dprintf(fd, "kind=%s signal=%d thread=%s\n", kind ? kind : "?", sig, pthread_main_np() ? "main" : "non-main");
    if (note != NULL && note[0] != '\0') {
        dprintf(fd, "note=%s\n", note);
    }
    // The main image's LOAD ADDRESS, not its slide: it is what `atos -l` wants,
    // and the difference (slide vs __TEXT vmaddr + slide) is a five-minute
    // mistake every single time. Symbolise a frame with
    //   atos -o Paperboat.app/Paperboat -arch arm64 -l <load address> <addr>
    dprintf(fd, "--- backtrace (image load address %p; atos -o <app>/Paperboat -arch arm64 -l <load address>) ---\n",
            (const void*)_dyld_get_image_header(0));
    PBIos_WriteFrameAddresses(fd, uap);
    fsync(fd);
    close(fd);
}

static void PBIos_SignalHandler(int sig, siginfo_t* info, void* uap) {
    (void)info;
    PBIos_WriteCrashRecord("signal", sig, NULL, uap);
    signal(sig, SIG_DFL);
    raise(sig); // and let the system write its own .ips beside our record
}

static void PBIos_ObjCExceptionHandler(NSException* e) {
    char note[256];
    snprintf(note, sizeof(note), "NSException %s: %s", e.name.UTF8String ?: "?", e.reason.UTF8String ?: "");
    PBIos_WriteCrashRecord("objc-exception", 0, note, NULL);
}

static void PBIos_TerminateHandler(void) {
    PBIos_WriteCrashRecord("cxx-terminate", 0, NULL, NULL);
    abort();
}

static void PBIos_AtExitHandler(void) {
    if (sPBCrashWritten != 0) {
        return; // we are already on the way out through a crash
    }
    // Not a crash — but on this port exit(0) is a real runtime path (LUS's
    // Context.cpp calls it on __IOS__ when pm64.o2r is missing), and "the app
    // closed itself" with no record is indistinguishable from a crash to
    // everyone who has to debug it.
    PBIos_WriteCrashRecord("exit", 0, "process exited normally (exit/return from main)", NULL);
}

static void PBIos_InstallCrashHandler(void) {
    PBIos_CrashPaths();
    static char altStack[SIGSTKSZ * 4];
    stack_t ss = { .ss_sp = altStack, .ss_size = sizeof(altStack), .ss_flags = 0 };
    sigaltstack(&ss, NULL);

    const int sigs[] = { SIGSEGV, SIGABRT, SIGBUS, SIGILL, SIGFPE, SIGTRAP, SIGSYS };
    for (size_t i = 0; i < sizeof(sigs) / sizeof(sigs[0]); i++) {
        struct sigaction sa;
        memset(&sa, 0, sizeof(sa));
        sa.sa_sigaction = PBIos_SignalHandler;
        // SA_SIGINFO for the ucontext (the ONLY usable source of a backtrace
        // from the alternate stack), SA_ONSTACK so a stack-overflow SIGSEGV
        // still has room to run, SA_RESETHAND so the re-raise below takes the
        // default action and the system writes its own report too.
        sa.sa_flags = SA_ONSTACK | SA_RESETHAND | SA_SIGINFO;
        sigemptyset(&sa.sa_mask);
        sigaction(sigs[i], &sa, NULL);
    }
    // Warm backtrace() up now, on a healthy stack: its first call lazily
    // initialises dyld unwinder state, and doing that for the first time inside
    // a signal handler is a good way to get an empty backtrace.
    {
        void* warm[4];
        (void)backtrace(warm, 4);
    }
    NSSetUncaughtExceptionHandler(PBIos_ObjCExceptionHandler);
    // std::set_terminate from a plain-C translation unit: the Itanium ABI
    // mangling is stable and libc++ exports it on every Apple platform, so the
    // handler is installed by asm name rather than compiling this whole file as
    // Objective-C++ (which is exactly what PaperBoatIosConsole.cpp exists to
    // avoid).
    {
        typedef void (*pb_terminate_fn)(void);
        extern pb_terminate_fn pb_set_terminate(pb_terminate_fn) __asm("__ZSt13set_terminatePFvvE");
        pb_set_terminate(PBIos_TerminateHandler);
    }
    atexit(PBIos_AtExitHandler);
    NSLog(@"[PaperBoatIosShell] crash capture armed → %s", sPBCrashPath);
}

// ---------------------------------------------------------------------------
// Remote console bridge — launch-gated TCP on :8771 (spec D3).
//
// It is an UNAUTHENTICATED command server: it can set any CVar and read
// crash.txt out of the container. So it is COMPILED OUT unless the build asks
// for it (-DPAPERBOAT_REMOTE_CONSOLE=ON, which scripts/build-sim.sh sets and a
// release build will not), and even when compiled in it does not listen until
// something asks: the PAPERBOAT_CONSOLE environment variable, a
// `console_enabled` file in Documents (creatable from the Files app, so an OTA
// build can opt in with no computer), or a paperboat://console deep link.
//
// Protocol and command list: docs/console-bridge.md.
// ---------------------------------------------------------------------------

#ifndef PAPERBOAT_REMOTE_CONSOLE
#define PAPERBOAT_REMOTE_CONSOLE 0
#endif

#if PAPERBOAT_REMOTE_CONSOLE

#define PBIOS_CONSOLE_PORT 8771

// A request handed from the socket thread to the main thread. Heap-allocated
// and NOT freed if the main thread misses its deadline: a 4 KB leak is the
// correct price for never writing into a dead stack frame if the game loop
// unblocks late.
typedef struct {
    volatile int done;
    int rc;
    char line[512];
    char out[4096];
} PBConsoleReq;

// LUS's CVar table has no locking whatsoever (upstream ships none; the
// SoH port's overlay 0030 recursive_mutex is not ported here yet), so running a
// command on the socket thread would be a concurrent find()/rehash against the
// game loop — a SEGV that looks like a game bug. Hop to the main thread, which
// IS the game loop on iOS, and poll. Never dispatch_sync: the game loop owns
// the main thread and a sync from here would deadlock the moment it blocks.
static NSString* PBIos_RunEngineCommand(NSString* line) {
    PBConsoleReq* req = calloc(1, sizeof(PBConsoleReq));
    if (req == NULL) {
        return @"err out of memory";
    }
    snprintf(req->line, sizeof(req->line), "%s", line.UTF8String ?: "");
    dispatch_async(dispatch_get_main_queue(), ^{
        req->rc = PBIos_ConsoleRun(req->line, req->out, (int)sizeof(req->out));
        __sync_synchronize();
        req->done = 1;
    });
    for (int i = 0; i < 500 && req->done == 0; i++) {
        usleep(10 * 1000); // up to 5 s: an extraction-busy first run is slow
    }
    if (req->done == 0) {
        return @"err main thread did not answer in 5s (game loop blocked?)";
    }
    NSString* out = [NSString stringWithUTF8String:req->out] ?: @"";
    const int rc = req->rc;
    free(req);
    if (rc == -1) {
        return @"err engine not ready";
    }
    if (rc == -2) {
        return [NSString stringWithFormat:@"err %@", out.length ? out : @"unknown command"];
    }
    return [NSString stringWithFormat:@"ok rc=%d%@", rc, out.length ? [@"\n" stringByAppendingString:out] : @""];
}

static NSString* PBIos_ReadDocumentsFile(NSString* name) {
    NSString* path = [NSString stringWithFormat:@"%s/Documents/%@", getenv("HOME") ?: "/tmp", name];
    NSString* content = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:nil];
    return content.length ? [@"ok\n" stringByAppendingString:content] : [NSString stringWithFormat:@"ok (no %@)", name];
}

static NSString* PBIos_HandleConsoleLine(NSString* raw) {
    NSString* line = [raw stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    if (line.length == 0) {
        return @"err empty";
    }
    NSArray<NSString*>* tok = [line componentsSeparatedByString:@" "];
    NSString* cmd = tok[0].lowercaseString;

    if ([cmd isEqualToString:@"ping"]) {
        return @"ok";
    }
    if ([cmd isEqualToString:@"ver"]) {
        NSDictionary* info = NSBundle.mainBundle.infoDictionary;
        return [NSString stringWithFormat:@"ok stamp=%s bundle=%@ ver=%@ build=%@ os=%@ %@ ready=%d bg=%d",
                                          gPortBuildStamp, info[@"CFBundleIdentifier"] ?: @"?",
                                          info[@"CFBundleShortVersionString"] ?: @"?", info[@"CFBundleVersion"] ?: @"?",
                                          UIDevice.currentDevice.systemName, UIDevice.currentDevice.systemVersion,
                                          PBIos_EngineReady(), PBIos_IsBackgrounded()];
    }
    if ([cmd isEqualToString:@"thermal"]) {
        return [NSString stringWithFormat:@"ok thermal=%d", PBIos_ThermalState()];
    }
    if ([cmd isEqualToString:@"fps"] || [cmd isEqualToString:@"perf"]) {
        // The Phase 0.3 harness (overlay 0012). Hopped to the main thread for
        // the same reason every engine command is: the probe's rings are
        // written by the game loop, and the game loop is the main thread on
        // iOS. Not dispatch_sync — the game loop owns that thread.
        __block NSString* line = nil;
        dispatch_async(dispatch_get_main_queue(), ^{
            // 1024, not 512: the rev-2 report carries the audio reservoir and
            // the shader/texture ledgers as well (overlay 0012 rev2).
            char buf[1024];
            const int n = PBIos_PerfReport(buf, (int)sizeof(buf));
            line = n > 0 ? ([NSString stringWithUTF8String:buf] ?: @"(unprintable)") : @"(no data)";
            // Round 16: the refresh/target/step-down state rides along, because
            // reading `fps` without it is what cost a whole build cycle — a
            // correct-looking 60 fps with tps=15 is the half-speed bug and only
            // the target explains it.
            char rbuf[256];
            if (PBIos_RefreshReport(rbuf, (int)sizeof(rbuf)) > 0) {
                line = [line stringByAppendingFormat:@"\n%@", [NSString stringWithUTF8String:rbuf] ?: @""];
            }
        });
        for (int i = 0; i < 300 && line == nil; i++) {
            usleep(10 * 1000); // up to 3 s
        }
        if (line == nil) {
            return @"err main thread did not answer in 3s (game loop blocked?)";
        }
        return [NSString stringWithFormat:@"ok\n%@", line];
    }
    if ([cmd isEqualToString:@"refresh"]) {
        // The interpolation target and where it came from (overlay 0023 rev 3).
        // Main thread: the engine writes these from the game loop.
        __block NSString* line = nil;
        dispatch_async(dispatch_get_main_queue(), ^{
            char buf[256];
            const int n = PBIos_RefreshReport(buf, (int)sizeof(buf));
            line = n > 0 ? [NSString stringWithFormat:@"ok %@", [NSString stringWithUTF8String:buf] ?: @"?"]
                         : @"err no data (engine not up?)";
        });
        for (int i = 0; i < 300 && line == nil; i++) {
            usleep(10 * 1000); // up to 3 s
        }
        return line ?: @"err main thread did not answer in 3s (game loop blocked?)";
    }
    if ([cmd isEqualToString:@"safearea"]) {
        // The insets the engine's touch layout is being given (overlay 0013),
        // in the drawable pixels it lays out in. Hopped to the main thread
        // because UIWindow.safeAreaInsets is only meaningful there — and
        // because that is where the getter refreshes its cache.
        __block NSString* line = nil;
        dispatch_async(dispatch_get_main_queue(), ^{
            float t = 0, l = 0, b = 0, r = 0;
            PBIos_MarkSafeAreaDirty(); // the verb always reports live values
            PBIos_GetSafeAreaInsets(&t, &l, &b, &r);
            UIWindow* w = PBIos_SafeAreaProbe();
            const CGFloat scale =
                w.windowScene != nil ? w.windowScene.screen.nativeScale : UIScreen.mainScreen.nativeScale;
            line = [NSString stringWithFormat:@"ok safearea_px top=%.0f left=%.0f bottom=%.0f right=%.0f "
                                              @"scale=%.1f probe=%.0fx%.0fpt orient=%ld",
                                              t, l, b, r, (double)scale, w.frame.size.width, w.frame.size.height,
                                              (long)PBIos_ActiveWindowScene().interfaceOrientation];
        });
        for (int i = 0; i < 300 && line == nil; i++) {
            usleep(10 * 1000); // up to 3 s
        }
        return line ?: @"err main thread did not answer in 3s (game loop blocked?)";
    }
    if ([cmd isEqualToString:@"ssaa"]) {
        // What the interpreter is actually rendering at (overlay 0019): the
        // factor, where it came from, the drawable it is relative to, and the
        // effective mCurDimensions the engine published back. Main thread for
        // the same reason `safearea` is — the drawable size is read off a
        // CAMetalLayer and cached there.
        __block NSString* line = nil;
        dispatch_async(dispatch_get_main_queue(), ^{
            const float cvar = PBIos_CVarGetFloat("gSohIos.Supersample", 0.0f);
            const float factor = PBIos_SsaaFactor();
            const uint32_t px = PBIos_DrawablePixelWidth();
            line = [NSString stringWithFormat:@"ok ssaa factor=%.2f cvar=%.2f tier=%.2f "
                                              @"drawable_px=%.0fx%.0f long_edge=%u dims=%ux%u",
                                              (double)factor, (double)cvar, (double)PBIos_SsaaDeviceDefault(),
                                              (double)gPBDrawableW, (double)gPBDrawableH, px, gPBIosSsaaW,
                                              gPBIosSsaaH];
        });
        for (int i = 0; i < 300 && line == nil; i++) {
            usleep(10 * 1000); // up to 3 s
        }
        return line ?: @"err main thread did not answer in 3s (game loop blocked?)";
    }
    if ([cmd isEqualToString:@"touch"]) {
        // The unified touch layer's own probe: frame + every chip centre in
        // points (spec rule 6 — engine screenshots cannot see UIKit), plus
        // the customizer gates (idb taps on the simulator are unreliable).
        // Main thread: it walks UIKit state the overlay owns.
        NSArray<NSString*>* args = tok.count > 1 ? [tok subarrayWithRange:NSMakeRange(1, tok.count - 1)] : @[];
        __block NSString* line = nil;
        dispatch_async(dispatch_get_main_queue(), ^{ line = PBIos_TouchProbe(args); });
        for (int i = 0; i < 300 && line == nil; i++) {
            usleep(10 * 1000); // up to 3 s
        }
        return line ?: @"err main thread did not answer in 3s (game loop blocked?)";
    }
    if ([cmd isEqualToString:@"bg"]) {
        return [NSString stringWithFormat:@"ok backgrounded=%d", PBIos_IsBackgrounded()];
    }
    if ([cmd isEqualToString:@"flush"]) {
        // Runs the same path the resign-active hook does, so the flush can be
        // proven without backgrounding anything.
        __block int ok = -1;
        dispatch_async(dispatch_get_main_queue(), ^{ ok = PBIos_EngineReady() ? PBIos_ConfigSave() : -2; });
        for (int i = 0; i < 300 && ok == -1; i++) {
            usleep(10 * 1000);
        }
        return [NSString stringWithFormat:@"ok flush=%d", ok];
    }
    if ([cmd isEqualToString:@"crashlog"]) {
        return PBIos_ReadDocumentsFile(@"crash.txt");
    }
    if ([cmd isEqualToString:@"texcache"]) {
        // Overlay 0037's ledger. Main thread, like `fps`: the counters are
        // written by the game loop.
        __block NSString* reply = nil;
        dispatch_async(dispatch_get_main_queue(), ^{
            char buf[256];
            const int n = PBIos_TexCacheReport(buf, (int)sizeof(buf));
            reply = n > 0 ? [NSString stringWithFormat:@"ok texcache %s", buf] : @"err no texcache data";
        });
        for (int i = 0; i < 300 && reply == nil; i++) {
            usleep(10 * 1000);
        }
        return reply ?: @"err main thread did not answer in 3s";
    }
#if PAPERBOAT_REMOTE_CONSOLE
    if ([cmd isEqualToString:@"pad"]) {
        // The physical-controller harness (D33). Every subcommand runs on the
        // main thread: SDL's joystick state is the game loop's, and so is the
        // engine state the reply reads back.
        NSString* sub = tok.count > 1 ? tok[1].lowercaseString : @"";
        __block NSString* reply = nil;
        dispatch_async(dispatch_get_main_queue(), ^{
            int rc = 1;
            if ([sub isEqualToString:@"attach"]) {
                rc = PBIos_PadAttach();
            } else if ([sub isEqualToString:@"detach"]) {
                rc = PBIos_PadDetach();
            } else if ([sub isEqualToString:@"btn"] && tok.count >= 4) {
                rc = PBIos_PadButton(tok[2].lowercaseString.UTF8String, tok[3].intValue);
            } else if (([sub isEqualToString:@"stick"] || [sub isEqualToString:@"rstick"]) && tok.count >= 4) {
                rc = PBIos_PadStick([sub isEqualToString:@"rstick"] ? "r" : "l", tok[2].floatValue,
                                    tok[3].floatValue);
            } else if (sub.length > 0) {
                rc = -1;
            }
            if (rc < 0) {
                reply = @"err usage: pad | pad attach | pad detach | pad btn "
                        @"<a|b|z|l|r|start|cu|cd|cl|cr|du|dd|dl|dr> <0|1> | pad stick <x> <y> | pad rstick <x> <y>";
                return;
            }
            // Always answer with the resulting state, so one command is both
            // the stimulus and the assertion (the siblings' injection rule).
            char buf[384];
            const int n = PBIos_PadReport(buf, (int)sizeof(buf));
            reply = [NSString stringWithFormat:@"%@ pad %s", rc ? @"ok" : @"err", n > 0 ? buf : "(no data)"];
        });
        for (int i = 0; i < 300 && reply == nil; i++) {
            usleep(10 * 1000);
        }
        return reply ?: @"err main thread did not answer in 3s";
    }
#endif
    if ([cmd isEqualToString:@"crashtest"]) {
        // Reply FIRST, crash a beat later: a crash inside the handler would
        // take the reply with it, and "did the command arrive" is exactly what
        // the test needs to know.
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(700 * NSEC_PER_MSEC)), dispatch_get_main_queue(), ^{
            char out[256];
            NSLog(@"[PaperBoatIosShell] crashtest: crashing deliberately now");
            PBIos_ConsoleRun("crashtest", out, (int)sizeof(out));
            // If the engine was not up, the console command does not exist —
            // crash here instead, which tests the same handler.
            volatile int* nowhere = NULL;
            *nowhere = 0x50424f41;
        });
        return @"ok crashing in 700ms (read crash.txt from the container afterwards)";
    }
    if ([cmd isEqualToString:@"help"] && tok.count == 1) {
        return @"ok bridge commands: ping ver fps thermal safearea ssaa touch pad texcache bg flush crashlog "
               @"crashtest help quit\n"
               @"     anything else is passed to libultraship's console (try: help lus, get <cvar>, set <cvar> <v>)";
    }
    if ([cmd isEqualToString:@"help"] && tok.count > 1) {
        return PBIos_RunEngineCommand(@"help");
    }
    if ([cmd isEqualToString:@"quit"] || [cmd isEqualToString:@"exit"]) {
        return @"bye";
    }
    // Everything else is the engine's: `set gTouchControls.Enabled 0`,
    // `get gTouchControls.Enabled`, `bind`, and whatever the port registers.
    return PBIos_RunEngineCommand(line);
}

static void PBIos_ServeClient(int cli) {
    // A client that hangs up before a slow reply lands raises SIGPIPE, whose
    // default action kills the app outright — and it would do it with no
    // crash.txt worth reading (SYNC-WAVE-2; sim-verified kill on SpaghettiKart).
    int nosig = 1;
    setsockopt(cli, SOL_SOCKET, SO_NOSIGPIPE, &nosig, sizeof(nosig));
    NSLog(@"[PaperBoatIosShell] bridge client connected");
    FILE* f = fdopen(cli, "r");
    char line[512];
    while (f != NULL && fgets(line, sizeof(line), f) != NULL) {
        @autoreleasepool {
            NSString* resp = PBIos_HandleConsoleLine([NSString stringWithUTF8String:line] ?: @"");
            dprintf(cli, "%s\n", resp.UTF8String);
            if ([resp isEqualToString:@"bye"]) {
                break;
            }
        }
    }
    if (f != NULL) {
        fclose(f); // closes cli
    } else {
        close(cli);
    }
    NSLog(@"[PaperBoatIosShell] bridge client disconnected");
}

static void PBIos_StartConsoleBridge(BOOL force) {
    static BOOL started = NO; // every caller is on the main thread
    if (started) {
        return;
    }
    BOOL fileGate = NO;
    const char* home = getenv("HOME");
    if (home != NULL) {
        NSString* docs = [NSString stringWithFormat:@"%s/Documents", home];
        NSFileManager* fm = NSFileManager.defaultManager;
        // .txt accepted too: iOS Files cannot create an extensionless file
        // without a rename dance.
        fileGate = [fm fileExistsAtPath:[docs stringByAppendingPathComponent:@"console_enabled"]] ||
                   [fm fileExistsAtPath:[docs stringByAppendingPathComponent:@"console_enabled.txt"]];
    }
    if (!force && getenv("PAPERBOAT_CONSOLE") == NULL && !fileGate) {
        return;
    }
    started = YES;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        const int srv = socket(AF_INET, SOCK_STREAM, 0);
        if (srv < 0) {
            NSLog(@"[PaperBoatIosShell] bridge socket failed: %d", errno);
            return;
        }
        int one = 1;
        // Without SO_REUSEADDR the port sits in TIME_WAIT for a minute after
        // every relaunch, which on a device loop means every second run has no
        // bridge at all.
        setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        struct sockaddr_in addr;
        memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = INADDR_ANY;
        addr.sin_port = htons(PBIOS_CONSOLE_PORT);
        BOOL bound = NO;
        for (int i = 0; i < 10 && !bound; i++) {
            bound = bind(srv, (struct sockaddr*)&addr, sizeof(addr)) == 0;
            if (!bound) {
                usleep(500 * 1000); // the previous run's socket may still be dying
            }
        }
        if (!bound || listen(srv, 1) != 0) {
            NSLog(@"[PaperBoatIosShell] bridge bind/listen failed on :%d: %d", PBIOS_CONSOLE_PORT, errno);
            close(srv);
            return;
        }
        NSLog(@"[PaperBoatIosShell] console bridge listening on :%d", PBIOS_CONSOLE_PORT);
        for (;;) {
            const int cli = accept(srv, NULL, NULL);
            if (cli < 0) {
                if (errno == EINTR) {
                    continue;
                }
                NSLog(@"[PaperBoatIosShell] bridge accept failed: %d", errno);
                usleep(100 * 1000);
                continue;
            }
            PBIos_ServeClient(cli);
        }
    });
}

#else // !PAPERBOAT_REMOTE_CONSOLE — a release build has no listener to enable.

static void PBIos_StartConsoleBridge(BOOL force) {
    (void)force;
}

#endif

// The runtime gate, third door: the `Remote Console` checkbox in
// Settings > iOS > Advanced (overlay 0021, gSohIos.RemoteConsole).
//
// WATCHED, NOT READ ONCE, and the choice is deliberate. The bridge starts from
// +load, long before the engine — and therefore before LUS has parsed
// paperboat.cfg.json — so a launch-time read would always see the CVar unset.
// Parsing the config file ourselves would mean a second JSON reader in the
// shell that has to stay in step with LUS's schema. A 2 s main-queue poll costs
// one CVar lookup per tick of a timer and needs to know nothing about the file
// format. It stops itself the moment the bridge is up.
//
// The env var and Documents/console_enabled gates are unchanged and still take
// effect earlier than this one. Turning the checkbox back OFF does not stop a
// listener that is already running: the socket thread has no cancellation path
// and inventing one to close a door the player just opened is not worth the
// bug surface. The tooltip says so.
static void PBIos_WatchRemoteConsoleCVar(void) {
#if PAPERBOAT_REMOTE_CONSOLE
    dispatch_async(dispatch_get_main_queue(), ^{
        static NSTimer* timer = nil;
        if (timer != nil) {
            return;
        }
        timer = [NSTimer scheduledTimerWithTimeInterval:2.0
                                                repeats:YES
                                                  block:^(NSTimer* t) {
                                                      if (!PBIos_EngineReady()) {
                                                          return;
                                                      }
                                                      if (PBIos_CVarGetInt("gSohIos.RemoteConsole", 0)) {
                                                          NSLog(@"[PaperBoatIosShell] gSohIos.RemoteConsole is set; "
                                                                @"starting the bridge");
                                                          PBIos_StartConsoleBridge(YES);
                                                          [t invalidate];
                                                      }
                                                  }];
    });
#endif
}

// ---------------------------------------------------------------------------
// Deep links — paperboat://
//
// The program pattern: a link may arrive at ANY lifecycle point, including
// before the engine exists (a cold launch through the link), so it is queued
// and consumed when the engine is ready rather than executed on arrival.
//
// PaperBoat's shell has no frame-driver hook to drain the queue from — SDL owns
// the loop and this port deliberately does not graft the SoH port's host view
// controller (D6) — so the drain is a main-queue retry instead. That is
// equivalent here: SDL's UIKit backend pumps the main run loop every frame, so
// a main-queue block runs on the game loop's own thread, between frames, which
// is exactly where the SoH port's frame-driver drain runs. See
// the shell divergence notes.
// ---------------------------------------------------------------------------

static NSMutableArray<NSURL*>* gPBPendingLinks = nil;

static void PBIos_ConsumeDeepLink(NSURL* url) {
    NSString* host = url.host.lowercaseString ?: @"";
    NSLog(@"[PaperBoatIosShell] deep link: %@", url.absoluteString);

    if ([host isEqualToString:@"launch"]) {
        // Deliberately does nothing beyond existing: opening the URL already
        // launched (or foregrounded) the app, which is the whole verb. It is
        // the one-tap entry point an App Intent / Shortcut / home-screen link
        // targets, and it is what run-sim.sh can use to prove URL delivery.
        NSLog(@"[PaperBoatIosShell] paperboat://launch — app is up (engine ready=%d)", PBIos_EngineReady());
        return;
    }
#if PAPERBOAT_REMOTE_CONSOLE
    if ([host isEqualToString:@"console"]) {
        PBIos_StartConsoleBridge(YES); // one-tap opt-in on a device with no Mac
        NSURLComponents* comps = [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO];
        for (NSURLQueryItem* item in comps.queryItems) {
            if ([item.name isEqualToString:@"cmd"] && item.value.length > 0) {
                char out[2048];
                const int rc = PBIos_ConsoleRun(item.value.UTF8String, out, (int)sizeof(out));
                NSLog(@"[PaperBoatIosShell] deep-link console '%@' rc=%d: %s", item.value, rc, out);
            }
        }
        return;
    }
#endif
    NSLog(@"[PaperBoatIosShell] unknown deep link ignored: %@", url.absoluteString);
}

static void PBIos_DrainDeepLinks(int attempt) {
    if (gPBPendingLinks.count == 0) {
        return;
    }
    // paperboat://console needs no engine (it starts a socket); everything else
    // wants a live console. Wait up to ~60 s for the first frame — a cold first
    // run extracts pm64.o2r for ~9 s before the engine exists at all, and a
    // link that arrives during extraction must not be silently dropped.
    const BOOL ready = PBIos_EngineReady() != 0;
    NSArray<NSURL*>* batch = [gPBPendingLinks copy];
    NSMutableArray<NSURL*>* stillPending = [NSMutableArray array];
    for (NSURL* url in batch) {
        const BOOL needsEngine = ![url.host.lowercaseString isEqualToString:@"console"];
        if (needsEngine && !ready && attempt < 240) {
            [stillPending addObject:url];
            continue;
        }
        PBIos_ConsumeDeepLink(url);
    }
    gPBPendingLinks = stillPending;
    if (gPBPendingLinks.count > 0) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(250 * NSEC_PER_MSEC)), dispatch_get_main_queue(),
                       ^{ PBIos_DrainDeepLinks(attempt + 1); });
    }
}

// WHERE THE URL ACTUALLY ARRIVES (measured, round 4).
//
// The SoH port's finding is that a scene-based app never sees the legacy
// `application:openURL:options:`. That is NOT what this port does, and the
// difference is instructive: the SoH port installs its scene delegate at
// RUNTIME on a scene UIKit created with delegate == nil, so there is nothing
// else to route to. Here the delegate is named by the plist AND SDL's own app
// delegate implements `application:openURL:options:` (SDL_uikitappdelegate.m
// → sendDropFileForURL) — and UIKit calls SDL's, not our
// `scene:openURLContexts:`. Verified on the iPhone Air simulator: with only the
// scene method implemented, `simctl openurl paperboat://launch` produced no
// delegate callback at all, and the URL went to SDL as an SDL_DROPFILE, which
// LUS hands to its FileDropMgr as if it were a file path.
//
// So the port takes the URL where it lands: a swizzle of SDL's delegate method,
// the same technique (and for the same reason) as the UIWindow graft. Ours
// consumes paperboat:// and forwards everything else to SDL untouched, so a
// real dropped file still behaves exactly as upstream intends.
// `scene:openURLContexts:` stays in place as well — it costs nothing and it is
// the documented path, so a future SDL with scene support, or a runtime that
// routes differently, is already handled.
static BOOL (*gPBOrigOpenURL)(id, SEL, UIApplication*, NSURL*, NSDictionary*) = NULL;

static BOOL PBIos_AppDelegateOpenURL(id self, SEL _cmd, UIApplication* app, NSURL* url, NSDictionary* options) {
    if ([url.scheme.lowercaseString isEqualToString:@"paperboat"]) {
        PBIos_QueueDeepLink(url);
        return YES; // consumed: SDL must not see it as a dropped file
    }
    if (gPBOrigOpenURL != NULL) {
        return gPBOrigOpenURL(self, _cmd, app, url, options);
    }
    return NO;
}

static void PBIos_InstallOpenURLHook(void) {
    Class sdlDelegate = objc_getClass("SDLUIKitDelegate");
    if (sdlDelegate == nil) {
        NSLog(@"[PaperBoatIosShell] WARNING: SDLUIKitDelegate not found — paperboat:// links will only "
              @"arrive through scene:openURLContexts:");
        return;
    }
    SEL sel = @selector(application:openURL:options:);
    Method m = class_getInstanceMethod(sdlDelegate, sel);
    if (m == NULL) {
        NSLog(@"[PaperBoatIosShell] WARNING: SDLUIKitDelegate has no %@", NSStringFromSelector(sel));
        return;
    }
    gPBOrigOpenURL = (BOOL (*)(id, SEL, UIApplication*, NSURL*, NSDictionary*))method_getImplementation(m);
    method_setImplementation(m, (IMP)PBIos_AppDelegateOpenURL);
    NSLog(@"[PaperBoatIosShell] openURL hook installed on SDLUIKitDelegate");
}

static void PBIos_QueueDeepLink(NSURL* url) {
    if (url == nil) {
        return;
    }
    if (gPBPendingLinks == nil) {
        gPBPendingLinks = [NSMutableArray array];
    }
    [gPBPendingLinks addObject:url];
    NSLog(@"[PaperBoatIosShell] deep link queued: %@ (pending %lu)", url.absoluteString,
          (unsigned long)gPBPendingLinks.count);
    dispatch_async(dispatch_get_main_queue(), ^{ PBIos_DrainDeepLinks(0); });
}

// App Intents (Shortcuts / Siri "Launch PaperBoat") hand their request over
// through the shared UserDefaults key below and open the app; the intent itself
// is Swift and lives in app/ios/PaperBoatIntents.swift. Consuming it here keeps
// one code path for "something outside the app asked us to do X".
static void PBIos_ConsumePendingIntent(void) {
    NSUserDefaults* defaults = NSUserDefaults.standardUserDefaults;
    NSString* pending = [defaults stringForKey:@"paperboat_pending_link"];
    if (pending.length == 0) {
        return;
    }
    [defaults removeObjectForKey:@"paperboat_pending_link"];
    [defaults synchronize];
    NSURL* url = [NSURL URLWithString:pending];
    NSLog(@"[PaperBoatIosShell] app intent pending link: %@", pending);
    PBIos_QueueDeepLink(url ?: [NSURL URLWithString:@"paperboat://launch"]);
}

// ---------------------------------------------------------------------------
// THE PROGRAM'S UNIFIED TOUCH LAYER (round 12, the design notes D25)
//
// WHY THIS EXISTS AT ALL, GIVEN UPSTREAM HAS A PAD
// -----------------------------------------------
// Rounds 6-7 kept upstream's engine-drawn pad and re-tuned its defaults onto
// the family's unified table (overlays 0013/0014, D17). The user tested 0.0.0.5
// and the verdict was "the touch controls are COMPLETELY different than what we
// have for other ports" — Q-004's default is vetoed. Matching a table is not
// matching a CONTROL: the feel (floating stick travel, hit radii, slide-across,
// the editor's chrome and eye chips) is the thing six shipped siblings share,
// and it lives in their UIKit overlay, not in their coordinates. One piece of
// that feel is deliberately NOT carried: the siblings' double-tap-to-lock Z
// (see `applyButton:`), because PM64 never holds Z.
//
// So this is the siblings' `SohIosTouchOverlay` (reference: Lighthouse-ios
// app/ios/SohIosShell.m :1378-3300), carried over with the adaptations the
// brief in docs/touch-layer-port-brief.md lists. Upstream's pad stays compiled
// in and one checkbox away (Settings > Controls > "Enable Touch Controls",
// `gTouchControls.Enabled`); overlay 0024 flips its default to 0 on iOS,
// because two pads at once is a half-working pad.
//
// THREE THINGS THAT ARE NOT THE SIBLINGS'
// ---------------------------------------
// 1. INJECTION. The siblings drive an SDL virtual joystick. This port has a
//    better seam: `GameEngine_ReadController` already calls the engine's own
//    `TouchControls_ApplyPad(pads)`, so overlay 0024 adds a second call that
//    merges THIS layer's state straight into `OSContPad`. No SDL joystick lock
//    contention (a sibling audio-hitch cause), no ControlDeck default-mapping
//    dependency, no trigger half-press workaround.
// 2. SAMPLING. `nuContDataGet` reads the pad once per 30 Hz game tick, and
//    `GameEngine_ReadController` is called from several entry points per frame.
//    A press that begins and ends between two ticks would simply not exist, so
//    every release arms a short EXPIRY (60 ms ~ 1.8 ticks) during which the bit
//    is still published. Latching on "consume" instead would hide the press
//    from every reader after the first one in the same frame.
// 3. MENU PASSTHROUGH IS INVERTED. The SoH port's overlay returns YES from
//    `pointInside:` while the menu is open and routes the touches itself. Here
//    overlay 0016 already reads menu swipes off ImGui's synthesised mouse, so
//    this overlay must return NO and get out of the way, or 0016/0017 die.
//
// Everything is in POINTS (UIKit's space). The engine's half of the port runs
// in drawable PIXELS (D16/D24) — nothing crosses that boundary here except the
// safe-area insets, which are converted once by PBIos_GetSafeAreaInsetsPoints.
// ---------------------------------------------------------------------------

#include <stdatomic.h>

// Overlay 0024's engine-side exports. All three are safe before the engine is
// up (they answer "no" / do nothing).
extern int PBIos_IsMenuOpen(void);
extern int PBIos_IsPopupOpen(void);
extern void PBIos_ToggleMenu(void);
// CVar writes, from the shell's one C++ translation unit.
extern void PBIos_CVarSetFloat(const char* name, float value);
extern void PBIos_CVarSetInt(const char* name, int value);
extern void PBIos_CVarClearBlock(const char* name);
extern void PBIos_CVarSave(void);

// --- The published pad state (read by overlay 0024 on the game tick) --------
//
// One writer (UIKit, main thread) and one reader (the game loop, which IS the
// main thread on iOS, plus whatever LUS asks from). `_Atomic` rather than a
// lock: the reader tolerates a frame of staleness by construction, and a lock
// taken on the audio/game path is exactly the hitch the siblings paid for.
static _Atomic unsigned short gPBPadHeld = 0;    // bits currently under a finger
static _Atomic signed char gPBPadStickX = 0;
static _Atomic signed char gPBPadStickY = 0;
static _Atomic int gPBPadLayerActive = 0;        // this layer owns input right now
// Release expiries, one per N64 button bit. UIKit writes them on the main
// thread; PBIos_ShellPadState is called from whichever thread is running the
// game tick, so these are `_Atomic` for the same reason gPBPadHeld is — a
// torn/stale double here is a button that sticks or never registers.
static _Atomic double gPBPadExpiry[16];

// N64 pad bits (include/PR/os_cont.h:121-134). Named here rather than included
// so this file stays plain Objective-C with no engine headers.
#define PB_BTN_A 0x8000
#define PB_BTN_B 0x4000
#define PB_BTN_Z 0x2000
#define PB_BTN_START 0x1000
#define PB_BTN_DU 0x0800
#define PB_BTN_DD 0x0400
#define PB_BTN_DL 0x0200
#define PB_BTN_DR 0x0100
#define PB_BTN_L 0x0020
#define PB_BTN_R 0x0010
#define PB_BTN_CU 0x0008
#define PB_BTN_CD 0x0004
#define PB_BTN_CL 0x0002
#define PB_BTN_CR 0x0001

#define PB_STICK_MAX 80.0 // kStickMax, the N64 raw range the game reads

// Press hold-over. 60 ms is just under two 30 Hz ticks: long enough that a
// fast tap survives the sampler, short enough that it cannot read as a hold.
#define PB_PRESS_HOLD_SECONDS 0.060

static void PBIos_PadPress(unsigned short mask, BOOL down) {
    if (mask == 0) {
        return;
    }
    if (down) {
        atomic_fetch_or(&gPBPadHeld, mask);
        return;
    }
    atomic_fetch_and(&gPBPadHeld, (unsigned short)~mask);
    const double until = CACurrentMediaTime() + PB_PRESS_HOLD_SECONDS;
    for (int i = 0; i < 16; i++) {
        if (mask & (unsigned short)(1u << i)) {
            atomic_store(&gPBPadExpiry[i], until);
        }
    }
}

static void PBIos_PadReleaseAll(void) {
    atomic_store(&gPBPadHeld, 0);
    atomic_store(&gPBPadStickX, 0);
    atomic_store(&gPBPadStickY, 0);
}

// The engine's entry point (overlay 0024 declares this in TouchControls.cpp).
// Returns 0 when this layer is not driving input, in which case nothing is
// merged and the port behaves exactly as upstream does.
int PBIos_ShellPadState(unsigned short* buttons, signed char* sx, signed char* sy) {
    if (!atomic_load(&gPBPadLayerActive)) {
        return 0;
    }
    unsigned short b = atomic_load(&gPBPadHeld);
    const double now = CACurrentMediaTime();
    for (int i = 0; i < 16; i++) {
        if (atomic_load(&gPBPadExpiry[i]) > now) {
            b |= (unsigned short)(1u << i);
        }
    }
    if (buttons != NULL) {
        *buttons = b;
    }
    if (sx != NULL) {
        *sx = atomic_load(&gPBPadStickX);
    }
    if (sy != NULL) {
        *sy = atomic_load(&gPBPadStickY);
    }
    return 1;
}

// --- Safe area, in POINTS ---------------------------------------------------
// The probe window above publishes drawable PIXELS, because that is the space
// the engine lays out in on this fork (D16). This overlay is a UIView and lays
// out in points, so it divides the same numbers by the screen's native scale
// rather than keeping a second source of truth. Exported: the `safearea`
// bridge verb prints both halves.
void PBIos_GetSafeAreaInsetsPoints(float* top, float* left, float* bottom, float* right) {
    float t = 0, l = 0, b = 0, r = 0;
    PBIos_GetSafeAreaInsets(&t, &l, &b, &r);
    CGFloat s = UIScreen.mainScreen.nativeScale;
    if (!(s >= 1.0 && s <= 4.0)) {
        s = 1.0;
    }
    if (top != NULL) {
        *top = (float)(t / s);
    }
    if (left != NULL) {
        *left = (float)(l / s);
    }
    if (bottom != NULL) {
        *bottom = (float)(b / s);
    }
    if (right != NULL) {
        *right = (float)(r / s);
    }
}

// --- The overlay ------------------------------------------------------------

@interface PBIosTouchOverlay : UIView
@end

// __unsafe_unretained, not __strong: this is a stack scratch array filled and
// read inside one call. The colours are autoreleased and the labels are string
// literals, so both outlive every caller's frame, and ARC's rules for object
// pointers in C structs have moved between clang releases.
typedef struct {
    CGPoint center; // in this view's points
    CGFloat radius;
    __unsafe_unretained UIColor* color;
    __unsafe_unretained NSString* label;
    unsigned short mask;
} PBButton;

#define PB_MAX_BUTTONS 20

static NSString* PBIos_LayoutKey(NSString* label);

// Physical controller present -> the touch controls yield (the ≡ chip does
// not: it is the only touch route into the menu). Resolved DYNAMICALLY rather
// than by importing <GameController/GameController.h>: that header would put
// GCController's class symbol on this app's link line, and nothing else in this
// port links the framework. The simulator synthesises a generic keyboard
// passthrough pad whose vendorName is exactly "Gamepad"; real hardware reports
// its brand, so the generic name is filtered out (sibling finding, verbatim).
static BOOL PBIos_PhysicalControllerPresent(void) {
    // ROUND 22: SDL's view counts as well. A real pad reaches the game through
    // SDL's MFi backend (D33), so "the engine has a gamepad" is the condition
    // the touch layer should actually yield to — and it is the condition the
    // bridge's diagnostic pad can produce, which is what makes the hot-plug
    // behaviour assertable on a simulator with no hardware in the room.
    if (PBIos_PadConnectedCount() > 0) {
        return YES;
    }
    Class cls = objc_getClass("GCController");
    if (cls == nil) {
        return NO;
    }
    NSArray* controllers = [cls valueForKey:@"controllers"];
    for (id c in controllers) {
        NSString* vendor = nil;
        @try {
            vendor = [c valueForKey:@"vendorName"];
        } @catch (NSException* e) {
            vendor = nil;
        }
        if (![(vendor ?: @"") isEqualToString:@"Gamepad"]) {
            return YES;
        }
    }
    return NO;
}

@implementation PBIosTouchOverlay {
    CGPoint _stickBase;  // set where the finger lands (floating stick)
    CGFloat _stickBaseR; // full-deflection travel
    CGFloat _stickKnobR; // knob radius
    CGPoint _stickKnob;
    BOOL _stickActive;
    UITouch* __unsafe_unretained _stickTouch; // identity only, never dereferenced after end
    NSMutableDictionary<NSValue*, NSNumber*>* _touchButtons; // UITouch ptr -> button index
    BOOL _menuOpen;       // the port's ImGui menu is up: overlay stands down
    BOOL _popupOpen;      // any ImGui modal (the Torch first-run prompts)
    BOOL _controllerMode; // physical controller connected
    BOOL _layerOff;       // gSohIos.TouchLayer 0 — upstream's own pad is the pad
    BOOL _loggedPadMigration; // the one-shot "a saved Enabled=1 is being ignored" note
    BOOL _zHeld; // Z chip pressed-state tint; PM64's Z is plain momentary (no lock)
    // Layout customizer
    BOOL _editMode;
    NSMutableDictionary<NSString*, NSValue*>* _layoutOverrides; // key -> normalized center
    NSMutableSet<NSString*>* _layoutHidden;
    CGFloat _layoutScale;
    CGPoint _stickHome; // normalized; CGPointZero = default
    NSString* _editDrag;
    NSString* _editSelected;
    BOOL _editTriggerArmed;
    BOOL _loggedFrame;
}

// Rotation / scene resize arrives here first, and this is where the cached
// safe-area insets are invalidated (round 13). UIKit calls it on every geometry
// change of this view, which is pinned to the game window, so it is the same
// event the probe window needs to be re-measured for.
- (void)layoutSubviews {
    [super layoutSubviews];
    PBIos_MarkSafeAreaDirty();
}

// The rectangle the pad is laid out in: this view's FULL bounds.
//
// Round 16, the user's 0.0.0.9 report: "the padding is different from Ship of
// Harkinian". It was — this method used to subtract the safe-area insets
// (68 pt left/right on an iPhone Air), which pushed every edge chip inward by
// 68 pt and shrank the reference rect the unified table's margins are measured
// from. No sibling does that: Lighthouse's `buttonRects:count:`
// (`app/ios/SohIosShell.m` ~:2673) starts from `self.bounds` and ignores the
// insets entirely, and the unified table's own margins (80/60/85/105/…) ARE
// the safe-area allowance — they were device-tuned on notched phones.
//
// So the method stays as the single layout seam, but it is now the identity on
// self.bounds. The safe-area probe (`PBIos_GetSafeAreaInsetsPoints`, D16) is
// still the engine's inset source for upstream's pad (0013) and the ImGui menu
// (0017); only the shell's own pad stopped using it.
- (CGRect)layoutBounds {
    return self.bounds;
}

// The ≡ chip is always visible on this port. The siblings hide it during
// gameplay (they have IsGamePaused/IsTitleOrDemo exports from their engine
// patches); PaperBoat's menu is the only place the iOS settings live and a
// hidden chip is one more thing to explain, so it stays.
- (BOOL)menuButtonVisible {
    return YES;
}

- (BOOL)isButtonHidden:(NSString*)label {
    if ([label isEqualToString:@"≡"]) {
        return NO; // the only touch path into the menu: never hideable
    }
    return [_layoutHidden containsObject:PBIos_LayoutKey(label)];
}

- (BOOL)toggleHiddenForLabel:(NSString*)label {
    if ([label isEqualToString:@"≡"]) {
        return NO;
    }
    NSString* key = PBIos_LayoutKey(label);
    BOOL nowHidden = ![_layoutHidden containsObject:key];
    if (nowHidden) {
        [_layoutHidden addObject:key];
    } else {
        [_layoutHidden removeObject:key];
    }
    [self setNeedsDisplay];
    return nowHidden;
}

- (CGPoint)zButtonCenter {
    PBButton btns[PB_MAX_BUTTONS];
    int n = 0;
    [self buttonRects:btns count:&n];
    for (int i = 0; i < n; i++) {
        if ([btns[i].label isEqualToString:@"Z"]) {
            return btns[i].center;
        }
    }
    return CGPointMake(-1000, -1000);
}

- (instancetype)initWithFrame:(CGRect)frame {
    self = [super initWithFrame:frame];
    if (self) {
        self.backgroundColor = UIColor.clearColor;
        self.opaque = NO;
        self.contentMode = UIViewContentModeRedraw;
        self.multipleTouchEnabled = YES;
        self.userInteractionEnabled = YES;
        _stickBaseR = 54.0;
        _stickKnobR = 34.0;
        _touchButtons = [NSMutableDictionary dictionary];
        _layoutOverrides = [NSMutableDictionary dictionary];
        _layoutHidden = [NSMutableSet set];
        _layoutScale = 1.0;
        _stickHome = CGPointZero;
        [self loadLayoutFromCVars];
        // EditLayout is a transient trigger, not a setting: a stale persisted 1
        // (a config flush mid-edit) must never relaunch straight into the
        // customizer.
        PBIos_CVarSetInt("gSohIos.EditLayout", 0);
        __weak PBIosTouchOverlay* weakSelf = self;
        [NSTimer scheduledTimerWithTimeInterval:0.25
                                        repeats:YES
                                          block:^(NSTimer* t) { [weakSelf syncWithMenuState]; }];
    }
    return self;
}

// Haptic feedback on button down (gSohIos.Haptics: 0 off, 1 light, 2 strong).
- (void)hapticTap {
    const int mode = PBIos_CVarGetInt("gSohIos.Haptics", 1);
    if (mode <= 0) {
        return;
    }
    UIImpactFeedbackStyle style = (mode >= 2) ? UIImpactFeedbackStyleMedium : UIImpactFeedbackStyleLight;
    UIImpactFeedbackGenerator* gen = [[UIImpactFeedbackGenerator alloc] initWithStyle:style];
    [gen impactOccurred];
}

// The engine's state is authoritative for hiding: it catches every way the
// menu opens and closes (the ≡ chip, the menu's own X, a deep link), not just
// this overlay's own button.
- (void)syncWithMenuState {
    // The customizer trigger is an EDGE, and it has to be armed first.
    //
    // `initWithFrame:` clears the CVar, but the overlay is built before LUS has
    // necessarily parsed paperboat.cfg.json, so that clear can be overwritten
    // by a stale persisted 1 seconds later — and the app then drops into the
    // customizer on its own, which is the sibling bug the clear exists to
    // prevent (observed once here, round 12). So: the first value seen with the
    // engine actually up is treated as history, never as a request. Only a 1
    // that appears AFTER a 0 has been observed opens the editor.
    const int editTrigger = PBIos_CVarGetInt("gSohIos.EditLayout", 0);
    if (!_editTriggerArmed) {
        if (PBIos_EngineReady()) {
            if (editTrigger) {
                PBIos_CVarSetInt("gSohIos.EditLayout", 0);
                NSLog(@"[PaperBoatIosShell] stale gSohIos.EditLayout=1 at startup, ignored and cleared");
            } else {
                _editTriggerArmed = YES;
            }
        }
    } else if (editTrigger) {
        PBIos_CVarSetInt("gSohIos.EditLayout", 0);
        // The menu widget hides the menu itself before setting this; if some
        // other route set it with the menu up, close it so drags land here.
        if (PBIos_IsMenuOpen()) {
            PBIos_ToggleMenu();
        }
        _editMode = YES;
        _editSelected = nil;
        _menuOpen = NO;
        atomic_store(&gPBPadLayerActive, 0);
        [self releaseAllControls];
        [self setNeedsDisplay];
        NSLog(@"[PaperBoatIosShell] touch layout customizer entered");
        return;
    }
    if (_editMode) {
        // The customizer owns the screen, and nothing it does is game input:
        // clear the merge flag here rather than only in the store below, which
        // this early return skips.
        atomic_store(&gPBPadLayerActive, 0);
        return;
    }

    // gSohIos.TouchLayer is the ONE switch between the two pads (round 15, D27).
    //
    // It used to also read gTouchControls.Enabled here, and that was the bug
    // the user saw on 0.0.0.8: every config written before 0.0.0.6 carries
    // Enabled = 1, so this layer switched itself off the moment LUS parsed
    // paperboat.cfg.json - "correct for a second, then reverted". Overlay 0028
    // now suppresses upstream's pad while TouchLayer is set, and Settings >
    // Controls > "Use Upstream Touch Pad" is the only thing that clears it, so
    // both sides read the same single truth and neither can lose a race.
    const BOOL layerOff = PBIos_CVarGetInt("gSohIos.TouchLayer", 1) == 0;

    // Once per launch, on the first tick that finds the engine's CVars loaded:
    // say out loud that a saved gTouchControls.Enabled is being overridden.
    // There is nothing to migrate - TouchLayer stays at its default 1 and the
    // saved value is simply ignored while it is - but a silent override is the
    // kind of thing that costs an hour on the next report.
    if (!_loggedPadMigration && PBIos_EngineReady()) {
        _loggedPadMigration = YES;
        if (PBIos_CVarGetInt("gTouchControls.Enabled", 0) != 0 && !layerOff) {
            NSLog(@"[PaperBoatIosShell] saved gTouchControls.Enabled=1 found; upstream's engine-drawn pad is "
                  @"SUPPRESSED because gSohIos.TouchLayer=1 (Settings > Controls > Use Upstream Touch Pad switches)");
        }
    }
    const BOOL open = PBIos_IsMenuOpen() != 0;
    const BOOL popup = PBIos_IsPopupOpen() != 0;
    const BOOL controller = PBIos_PhysicalControllerPresent();
    BOOL changed = NO;

    if (layerOff != _layerOff) {
        _layerOff = layerOff;
        [self releaseAllControls];
        changed = YES;
        NSLog(@"[PaperBoatIosShell] shell touch layer %@ (gSohIos.TouchLayer=%d gTouchControls.Enabled=%d)",
              layerOff ? @"OFF" : @"ON", PBIos_CVarGetInt("gSohIos.TouchLayer", 1),
              PBIos_CVarGetInt("gTouchControls.Enabled", 0));
    }
    if (popup != _popupOpen) {
        _popupOpen = popup;
        if (popup) {
            [self releaseAllControls];
        }
        changed = YES;
    }
    if (controller != _controllerMode) {
        _controllerMode = controller;
        [self releaseAllControls];
        changed = YES;
        NSLog(@"[PaperBoatIosShell] controller mode %@", controller ? @"ON (touch yields)" : @"OFF");
    }
    if (open != _menuOpen) {
        _menuOpen = open;
        if (open) {
            [self releaseAllControls]; // no stuck buttons while the menu is up
        }
        changed = YES;
    }
    // Whether the engine may merge this layer's state at all.
    atomic_store(&gPBPadLayerActive, (!_layerOff && !_popupOpen && !_menuOpen && !_editMode) ? 1 : 0);

    if (changed) {
        [self setNeedsDisplay];
    }
    const CGFloat opacity = (CGFloat)PBIos_CVarGetFloat("gSohIos.TouchOpacity", 0.8f);
    self.alpha = _editMode ? 1.0 : MAX(0.25, MIN(1.0, opacity));
}

- (void)releaseAllControls {
    PBButton btns[PB_MAX_BUTTONS];
    int n = 0;
    [self buttonRects:btns count:&n];
    for (NSNumber* idx in _touchButtons.allValues) {
        const int i = idx.intValue;
        if (i >= 0 && i < n) {
            [self applyButton:btns[i].label down:NO];
        }
    }
    [_touchButtons removeAllObjects];
    _stickActive = NO;
    _stickTouch = nil;
    _zHeld = NO;
    PBIos_PadReleaseAll();
}

// Button label -> N64 pad bit. ≡ is not a pad bit; Z is the plain CONT_G bit
// (the siblings send a trigger axis because their virtual pad has one), and the
// C-buttons are four real bits rather than a right-stick emulation.
- (void)applyButton:(NSString*)label down:(BOOL)down {
    if ([label isEqualToString:@"≡"]) {
        if (down) {
            PBIos_ToggleMenu();
            _menuOpen = YES; // believed until the next sync tick confirms
            [self releaseAllControls];
            [self setNeedsDisplay];
        }
        return;
    }
    if ([label isEqualToString:@"Z"]) {
        // Touchscreen Z: PLAIN MOMENTARY, and deliberately NOT the siblings'
        // behaviour. Every sibling double-taps Z within 0.35 s into a held
        // lock, because their games hold Z for long stretches (Z-targeting,
        // crouch-strafe). Paper Mario 64 never needs Z held for more than a
        // press, and the user asked for the gesture to go (2026-09-17). Deleted
        // rather than defaulted off: a changed CVar default never beats a
        // value already saved in a user's config (the standing trap), so the
        // `gSohIos.ZDoubleTap` CVar is gone with it. Recorded as a family
        // delta in D25 and docs/touch-layer-port-brief.md.
        //
        // _zHeld survives purely as the chip's pressed-state tint.
        if (down) {
            _zHeld = YES;
            PBIos_PadPress(PB_BTN_Z, YES);
        } else {
            _zHeld = NO;
            PBIos_PadPress(PB_BTN_Z, NO);
        }
        [self setNeedsDisplay];
        return;
    }
    PBButton btns[PB_MAX_BUTTONS];
    int n = 0;
    [self buttonRects:btns count:&n];
    for (int i = 0; i < n; i++) {
        if ([btns[i].label isEqualToString:label]) {
            PBIos_PadPress(btns[i].mask, down);
            return;
        }
    }
}

- (int)hitButton:(CGPoint)p {
    PBButton btns[PB_MAX_BUTTONS];
    int n = 0;
    [self buttonRects:btns count:&n];
    for (int i = 0; i < n; i++) {
        if ([self isButtonHidden:btns[i].label]) {
            continue; // user-hidden: not drawn, not tappable
        }
        if (hypot(p.x - btns[i].center.x, p.y - btns[i].center.y) <= btns[i].radius * 1.35) {
            return i;
        }
    }
    return -1;
}

// ZERO effective deadzone (spec): a touch stick has no mechanical noise, so
// any deflection is meant. The siblings precompensate LUS's 20 % stick
// deadzone here because they inject through a virtual JOYSTICK; this layer
// writes OSContPad directly, downstream of every deadzone, so the value is the
// raw N64 range and nothing has to be undone.
static signed char PBIos_StickValue(CGFloat n) {
    if (n == 0) {
        return 0;
    }
    CGFloat m = MIN(1.0, fabs(n));
    if (PBIos_CVarGetInt("gSohIos.StickCurve", 0) == 1) {
        m = m * m; // expo: finer control near centre
    }
    const CGFloat v = m * PB_STICK_MAX;
    const long r = lround(n < 0 ? -v : v);
    return (signed char)MAX(-80L, MIN(80L, r));
}

- (void)updateStickAxesFromKnob {
    const CGFloat nx = (_stickKnob.x - _stickBase.x) / _stickBaseR;
    const CGFloat ny = (_stickKnob.y - _stickBase.y) / _stickBaseR;
    atomic_store(&gPBPadStickX, PBIos_StickValue(MAX(-1.0, MIN(1.0, nx))));
    // Screen y grows downward; the N64 stick's does not.
    atomic_store(&gPBPadStickY, PBIos_StickValue(-MAX(-1.0, MIN(1.0, ny))));
}

// --- Layout customizer ------------------------------------------------------

static NSString* PBIos_LayoutKey(NSString* label) {
    if ([label isEqualToString:@"C↑"]) return @"CU";
    if ([label isEqualToString:@"C↓"]) return @"CD";
    if ([label isEqualToString:@"C←"]) return @"CL";
    if ([label isEqualToString:@"C→"]) return @"CR";
    if ([label isEqualToString:@"≡"]) return @"MENU";
    return label;
}

static NSArray<NSString*>* PBIos_LayoutKeys(void) {
    return @[
        @"A", @"B", @"CU", @"CD", @"CL", @"CR", @"Z", @"L", @"R", @"START", @"MENU"
    ];
}

// Canonical layout reference: ALWAYS landscape-shaped, regardless of the
// view's momentary orientation — early-boot portrait bounds scrambled
// normalized coordinates on a sibling's device.
- (CGSize)layoutRefSize {
    CGRect b = [self layoutBounds];
    const CGFloat w = CGRectGetWidth(b), h = CGRectGetHeight(b);
    return CGSizeMake(MAX(w, h), MIN(w, h));
}

// Normalized coordinates are stored against the layout rect (now the full
// bounds), so they are read back relative to its origin too.
- (CGPoint)pointFromNormalized:(CGPoint)n {
    CGRect b = [self layoutBounds];
    CGSize ref = [self layoutRefSize];
    return [self applyLefty:CGPointMake(b.origin.x + n.x * ref.width, b.origin.y + n.y * ref.height)];
}

- (void)loadLayoutFromCVars {
    if (!PBIos_CVarGetInt("gSohIos.Layout.Set", 0)) {
        return;
    }
    _layoutScale = (CGFloat)PBIos_CVarGetFloat("gSohIos.Layout.Scale", 1.0f);
    for (NSString* key in PBIos_LayoutKeys()) {
        const float x =
            PBIos_CVarGetFloat([NSString stringWithFormat:@"gSohIos.Layout.%@.x", key].UTF8String, -1.0f);
        const float y =
            PBIos_CVarGetFloat([NSString stringWithFormat:@"gSohIos.Layout.%@.y", key].UTF8String, -1.0f);
        if (x >= 0 && y >= 0) {
            _layoutOverrides[key] = [NSValue valueWithCGPoint:CGPointMake(x, y)];
        }
        // MENU is never hideable, so a stale MENU.hidden is not read back.
        if (![key isEqualToString:@"MENU"] &&
            PBIos_CVarGetInt([NSString stringWithFormat:@"gSohIos.Layout.%@.hidden", key].UTF8String, 0)) {
            [_layoutHidden addObject:key];
        }
    }
    const float sx = PBIos_CVarGetFloat("gSohIos.Layout.Stick.x", -1.0f);
    const float sy = PBIos_CVarGetFloat("gSohIos.Layout.Stick.y", -1.0f);
    if (sx >= 0 && sy >= 0) {
        _stickHome = CGPointMake(sx, sy);
    }
}

- (void)saveLayoutToCVars {
    // Clear the whole block first: stale per-button keys from earlier saves
    // resurrected on the next launch on a sibling's device.
    PBIos_CVarClearBlock("gSohIos.Layout");
    // LUS's ClearBlock() ends with Load(), which re-reads EVERY CVar from
    // paperboat.cfg.json — so a `gSohIos.EditLayout` that is still 1 on disk
    // comes back to life here and the next sync tick re-opens the customizer
    // the player has just left. (Measured, round 12: "touch layout saved"
    // followed immediately by "customizer entered", every single save.) Re-
    // assert the transient trigger before writing, so the value that lands on
    // disk is 0 and the resurrection cannot repeat.
    PBIos_CVarSetInt("gSohIos.EditLayout", 0);
    PBIos_CVarSetInt("gSohIos.Layout.Set", 1);
    PBIos_CVarSetFloat("gSohIos.Layout.Scale", (float)_layoutScale);
    for (NSString* key in _layoutOverrides) {
        const CGPoint n = [_layoutOverrides[key] CGPointValue];
        PBIos_CVarSetFloat([NSString stringWithFormat:@"gSohIos.Layout.%@.x", key].UTF8String, (float)n.x);
        PBIos_CVarSetFloat([NSString stringWithFormat:@"gSohIos.Layout.%@.y", key].UTF8String, (float)n.y);
    }
    for (NSString* key in _layoutHidden) {
        PBIos_CVarSetInt([NSString stringWithFormat:@"gSohIos.Layout.%@.hidden", key].UTF8String, 1);
    }
    if (!CGPointEqualToPoint(_stickHome, CGPointZero)) {
        PBIos_CVarSetFloat("gSohIos.Layout.Stick.x", (float)_stickHome.x);
        PBIos_CVarSetFloat("gSohIos.Layout.Stick.y", (float)_stickHome.y);
    }
    PBIos_CVarSave();
    NSLog(@"[PaperBoatIosShell] touch layout saved (%lu moved, %lu hidden, scale %.2f)",
          (unsigned long)_layoutOverrides.count, (unsigned long)_layoutHidden.count, (double)_layoutScale);
}

- (CGPoint)stickHomePoint {
    CGRect b = [self layoutBounds];
    if (CGPointEqualToPoint(_stickHome, CGPointZero)) {
        return [self applyLefty:CGPointMake(b.origin.x + 150, CGRectGetMaxY(b) - 170)];
    }
    return [self pointFromNormalized:_stickHome];
}

// Left-handed mirror (gSohIos.LeftyFlip): flips every X around the safe rect.
// Applied AFTER overrides so customized layouts mirror too.
- (CGPoint)applyLefty:(CGPoint)c {
    if (!PBIos_CVarGetInt("gSohIos.LeftyFlip", 0)) {
        return c;
    }
    CGRect b = [self layoutBounds];
    return CGPointMake(CGRectGetMinX(b) + CGRectGetMaxX(b) - c.x, c.y);
}

#define kPBStickHaloR 150.0
#define kPBHideBadgeR 14.0

- (CGRect)editChromeRect {
    CGRect b = [self layoutBounds];
    return CGRectMake(b.origin.x + 16, CGRectGetMaxY(b) - 50, 330, 44);
}
- (CGPoint)editResetCenter {
    CGRect c = [self editChromeRect];
    return CGPointMake(CGRectGetMinX(c) + 26, CGRectGetMidY(c));
}
- (CGPoint)editSaveCenter {
    CGRect c = [self editChromeRect];
    return CGPointMake(CGRectGetMaxX(c) - 26, CGRectGetMidY(c));
}
- (CGRect)editSliderRect {
    CGRect c = [self editChromeRect];
    return CGRectMake(CGRectGetMinX(c) + 58, CGRectGetMidY(c) - 4, CGRectGetWidth(c) - 116, 8);
}

- (int)hitButtonForEdit:(CGPoint)pnt {
    PBButton btns[PB_MAX_BUTTONS];
    int n = 0;
    [self buttonRects:btns count:&n];
    for (int i = 0; i < n; i++) {
        if (hypot(pnt.x - btns[i].center.x, pnt.y - btns[i].center.y) <= MAX(btns[i].radius * 1.35, 30)) {
            return i;
        }
    }
    return -1;
}

// A small eye chip sits just OUTSIDE the button rim so the whole body stays a
// drag handle. It points away from the local cluster centroid so a tight group
// (the C diamond, the D-pad cross) spreads its badges outward, and is clamped
// on-screen so an edge button never pushes its badge off.
- (CGPoint)hideBadgeCenterForCenter:(CGPoint)c radius:(CGFloat)r {
    CGRect b = [self layoutBounds];
    PBButton all[PB_MAX_BUTTONS];
    int an = 0;
    [self buttonRects:all count:&an];
    CGFloat sx = 0, sy = 0;
    int cnt = 0;
    for (int i = 0; i < an; i++) {
        const CGFloat d = hypot(all[i].center.x - c.x, all[i].center.y - c.y);
        if (d > 1.0 && d < 150.0) {
            sx += all[i].center.x;
            sy += all[i].center.y;
            cnt++;
        }
    }
    CGFloat dirX, dirY;
    if (cnt > 0) {
        dirX = c.x - sx / cnt;
        dirY = c.y - sy / cnt;
        const CGFloat nrm = hypot(dirX, dirY);
        if (nrm < 1.0) {
            dirX = 0;
            dirY = -1;
        } else {
            dirX /= nrm;
            dirY /= nrm;
        }
    } else {
        dirX = (c.x > CGRectGetMidX(b)) ? -0.7071 : 0.7071;
        dirY = (c.y > CGRectGetMidY(b)) ? -0.7071 : 0.7071;
    }
    const CGFloat off = r + kPBHideBadgeR - 2.0;
    CGPoint badge = CGPointMake(c.x + dirX * off, c.y + dirY * off);
    const CGFloat m = kPBHideBadgeR + 4.0;
    badge.x = MAX(b.origin.x + m, MIN(CGRectGetMaxX(b) - m, badge.x));
    badge.y = MAX(b.origin.y + m, MIN(CGRectGetMaxY(b) - m, badge.y));
    return badge;
}

- (int)indexForLayoutKey:(NSString*)key {
    if (key == nil) {
        return -1;
    }
    PBButton btns[PB_MAX_BUTTONS];
    int n = 0;
    [self buttonRects:btns count:&n];
    for (int i = 0; i < n; i++) {
        if ([PBIos_LayoutKey(btns[i].label) isEqualToString:key]) {
            return i;
        }
    }
    return -1;
}

// The eye chip lives ONLY on the currently-selected button.
- (BOOL)pointHitsSelectedChip:(CGPoint)pnt center:(CGPoint*)outChip {
    const int i = [self indexForLayoutKey:_editSelected];
    if (i < 0) {
        return NO;
    }
    PBButton btns[PB_MAX_BUTTONS];
    int n = 0;
    [self buttonRects:btns count:&n];
    if ([btns[i].label isEqualToString:@"≡"]) {
        return NO; // never hideable, so no chip
    }
    const CGPoint chip = [self hideBadgeCenterForCenter:btns[i].center radius:btns[i].radius];
    if (outChip != NULL) {
        *outChip = chip;
    }
    return hypot(pnt.x - chip.x, pnt.y - chip.y) <= kPBHideBadgeR + 4.0;
}

- (void)drawHideBadgeAt:(CGPoint)c hidden:(BOOL)hidden {
    CGContextRef ctx = UIGraphicsGetCurrentContext();
    const CGRect ring = CGRectMake(c.x - kPBHideBadgeR, c.y - kPBHideBadgeR, kPBHideBadgeR * 2, kPBHideBadgeR * 2);
    [[UIColor colorWithWhite:0.10 alpha:0.92] setFill];
    CGContextFillEllipseInRect(ctx, ring);
    [[UIColor colorWithWhite:1.0 alpha:0.85] setStroke];
    CGContextSetLineWidth(ctx, 1.5);
    CGContextStrokeEllipseInRect(ctx, ring);
    UIImageSymbolConfiguration* cfg =
        [UIImageSymbolConfiguration configurationWithPointSize:15 weight:UIImageSymbolWeightSemibold];
    UIImage* img = [[UIImage systemImageNamed:(hidden ? @"eye.fill" : @"eye.slash.fill") withConfiguration:cfg]
        imageWithTintColor:UIColor.whiteColor
             renderingMode:UIImageRenderingModeAlwaysOriginal];
    if (img != nil) {
        [img drawInRect:CGRectMake(c.x - img.size.width / 2, c.y - img.size.height / 2, img.size.width,
                                   img.size.height)];
    } else {
        [self drawGlyph:(hidden ? @"o" : @"x") at:c size:16 alpha:1.0];
    }
}

// Bridge gate probe (idb taps on the simulator are unreliable — spec traps).
// MAIN THREAD ONLY. Subcommands mirror the siblings':
//   list | select KEY | chiptap | tapedit X Y | save | frame
- (NSString*)layoutProbe:(NSArray<NSString*>*)a {
    PBButton btns[PB_MAX_BUTTONS];
    int n = 0;
    [self buttonRects:btns count:&n];
    NSString* sub = a.count >= 1 ? a[0].lowercaseString : @"list";
    if ([sub isEqualToString:@"pad"]) {
        // Exactly the word overlay 0024 reads on the game tick, including the
        // ~60 ms release hold-over. `active=0` means nothing is merged at all.
        unsigned short buttons = 0;
        signed char psx = 0, psy = 0;
        const int active = PBIos_ShellPadState(&buttons, &psx, &psy);
        return [NSString stringWithFormat:@"ok pad active=%d buttons=0x%04x stick_x=%d stick_y=%d held=0x%04x",
                                          active, buttons, (int)psx, (int)psy,
                                          (unsigned)atomic_load(&gPBPadHeld)];
    }
    if ([sub isEqualToString:@"frame"]) {
        // The spec rule 6: the view logs its own frame and every chip centre,
        // because an engine screenshot cannot see UIKit.
        CGRect lb = [self layoutBounds];
        NSMutableString* s = [NSMutableString
            stringWithFormat:@"ok overlay frame=%.0f,%.0f %.0fx%.0f pt  safe=%.0f,%.0f %.0fx%.0f pt  scale=%.2f",
                             self.frame.origin.x, self.frame.origin.y, self.frame.size.width, self.frame.size.height,
                             lb.origin.x, lb.origin.y, lb.size.width, lb.size.height, (double)_layoutScale];
        for (int i = 0; i < n; i++) {
            [s appendFormat:@"\n  %-5s c=%.0f,%.0f r=%.0f hidden=%d inSafe=%d", PBIos_LayoutKey(btns[i].label).UTF8String,
                            btns[i].center.x, btns[i].center.y, btns[i].radius, [self isButtonHidden:btns[i].label],
                            CGRectContainsRect(lb, CGRectMake(btns[i].center.x - btns[i].radius,
                                                              btns[i].center.y - btns[i].radius, btns[i].radius * 2,
                                                              btns[i].radius * 2))];
        }
        CGPoint home = [self stickHomePoint];
        [s appendFormat:@"\n  STICK c=%.0f,%.0f halo=%.0f", home.x, home.y, (double)kPBStickHaloR];
        return s;
    }
    if ([sub isEqualToString:@"select"] && a.count >= 2) {
        const int i = [self indexForLayoutKey:a[1].uppercaseString];
        if (i < 0) {
            return @"err no such button";
        }
        [self editBegan:btns[i].center];
        NSString* dragWas = _editDrag ?: @"nil";
        [self editEnded];
        return [NSString stringWithFormat:@"ok sel=%@ editDragWas=%@", _editSelected ?: @"none", dragWas];
    }
    if ([sub isEqualToString:@"chiptap"]) {
        const int i = [self indexForLayoutKey:_editSelected];
        if (i < 0) {
            return @"err nothing selected";
        }
        CGPoint chip = CGPointZero;
        (void)[self pointHitsSelectedChip:btns[i].center center:&chip];
        const BOOL was = [self isButtonHidden:btns[i].label];
        [self editBegan:chip];
        NSString* dragAfter = _editDrag ?: @"nil";
        const BOOL now = [self isButtonHidden:btns[i].label];
        [self editEnded];
        return [NSString stringWithFormat:@"ok chiptap %@ %d->%d editDrag=%@", PBIos_LayoutKey(btns[i].label), was,
                                          now, dragAfter];
    }
    if ([sub isEqualToString:@"drag"] && a.count >= 4) {
        const int i = [self indexForLayoutKey:a[1].uppercaseString];
        if (i < 0) {
            return @"err no such button";
        }
        [self editBegan:btns[i].center];
        [self editMoved:CGPointMake(a[2].floatValue, a[3].floatValue)];
        [self editEnded];
        PBButton after[PB_MAX_BUTTONS];
        int an = 0;
        [self buttonRects:after count:&an];
        return [NSString stringWithFormat:@"ok dragged %@ to %.0f,%.0f", a[1].uppercaseString, after[i].center.x,
                                          after[i].center.y];
    }
    if ([sub isEqualToString:@"save"]) {
        [self saveLayoutToCVars];
        return @"ok saved";
    }
    if ([sub isEqualToString:@"reset"]) {
        [self editBegan:[self editResetCenter]];
        [self editEnded];
        return @"ok reset";
    }
    if ([sub isEqualToString:@"tapedit"] && a.count >= 3) {
        [self editBegan:CGPointMake(a[1].floatValue, a[2].floatValue)];
        NSString* drag = _editDrag ?: @"nil";
        [self editEnded];
        return [NSString stringWithFormat:@"ok tapedit editMode=%d editDrag=%@", _editMode, drag];
    }
    NSMutableString* s =
        [NSMutableString stringWithFormat:@"ok edit=%d sel=%@ menu=%d popup=%d layerOff=%d ctrl=%d active=%d",
                                          _editMode, _editSelected ?: @"none", _menuOpen, _popupOpen, _layerOff,
                                          _controllerMode, atomic_load(&gPBPadLayerActive)];
    for (int i = 0; i < n; i++) {
        [s appendFormat:@" %@:h%d", PBIos_LayoutKey(btns[i].label), [self isButtonHidden:btns[i].label]];
    }
    return s;
}

- (void)editBegan:(CGPoint)pnt {
    const CGPoint reset = [self editResetCenter], save = [self editSaveCenter];
    const CGRect slider = CGRectInset([self editSliderRect], -12, -18);
    if (hypot(pnt.x - reset.x, pnt.y - reset.y) <= 26) {
        [_layoutOverrides removeAllObjects];
        [_layoutHidden removeAllObjects]; // reset restores every button to visible
        _layoutScale = 1.0;
        _stickHome = CGPointZero;
        [self setNeedsDisplay];
        return;
    }
    if (hypot(pnt.x - save.x, pnt.y - save.y) <= 26) {
        [self saveLayoutToCVars];
        _editMode = NO;
        _editDrag = nil;
        [self setNeedsDisplay];
        return;
    }
    if (CGRectContainsPoint(slider, pnt)) {
        _editDrag = @"__slider";
        [self editMoved:pnt];
        return;
    }
    // The eye chip is checked BEFORE the body drag: tapping it toggles
    // visibility, keeps the selection, and never starts a drag.
    if ([self pointHitsSelectedChip:pnt center:NULL]) {
        const int i = [self indexForLayoutKey:_editSelected];
        PBButton bb[PB_MAX_BUTTONS];
        int bn = 0;
        [self buttonRects:bb count:&bn];
        if (i >= 0) {
            [self toggleHiddenForLabel:bb[i].label];
        }
        return;
    }
    const int idx = [self hitButtonForEdit:pnt];
    if (idx >= 0) {
        PBButton btns[PB_MAX_BUTTONS];
        int n = 0;
        [self buttonRects:btns count:&n];
        _editSelected = PBIos_LayoutKey(btns[idx].label);
        _editDrag = _editSelected;
        [self setNeedsDisplay];
        return;
    }
    const CGPoint home = [self stickHomePoint];
    if (hypot(pnt.x - home.x, pnt.y - home.y) <= kPBStickHaloR) {
        _editDrag = @"__stick";
    }
}

- (void)editMoved:(CGPoint)pnt {
    CGRect b = [self layoutBounds];
    if (_editDrag == nil) {
        return;
    }
    if ([_editDrag isEqualToString:@"__slider"]) {
        CGRect s = [self editSliderRect];
        const CGFloat frac = MAX(0.0, MIN(1.0, (pnt.x - CGRectGetMinX(s)) / CGRectGetWidth(s)));
        _layoutScale = 0.7 + frac * (1.4 - 0.7);
        [self setNeedsDisplay];
        return;
    }
    // Clamped to the SAFE rect, not the display: an unclamped drag writes a
    // position no later inset handling can rescue (overlay 0013's lesson).
    const CGFloat m = 30;
    CGPoint clamped = CGPointMake(MAX(b.origin.x + m, MIN(CGRectGetMaxX(b) - m, pnt.x)),
                                  MAX(b.origin.y + m, MIN(CGRectGetMaxY(b) - m, pnt.y)));
    clamped = [self applyLefty:clamped]; // un-mirror before storing
    CGSize ref = [self layoutRefSize];
    const CGPoint normalized =
        CGPointMake((clamped.x - b.origin.x) / ref.width, (clamped.y - b.origin.y) / ref.height);
    if ([_editDrag isEqualToString:@"__stick"]) {
        _stickHome = normalized;
    } else {
        _layoutOverrides[_editDrag] = [NSValue valueWithCGPoint:normalized];
    }
    [self setNeedsDisplay];
}

- (void)editEnded {
    _editDrag = nil;
}

// The stick is FLOATING: hidden until a touch lands inside its spawn halo
// (customizable home), then its base is wherever the finger came down.
- (BOOL)pointInStickRegion:(CGPoint)p {
    const CGPoint home = [self stickHomePoint];
    if (hypot(p.x - home.x, p.y - home.y) > kPBStickHaloR) {
        return NO;
    }
    // Z protection (sibling device feedback): a halo around Z never spawns the
    // stick — a missed Z tap is a dead tap, not a surprise stick.
    if (![self isButtonHidden:@"Z"]) {
        const CGPoint zC = [self zButtonCenter];
        if (hypot(p.x - zC.x, p.y - zC.y) <= (34 * 1.35 + 26) * _layoutScale) {
            return NO;
        }
    }
    return YES;
}

- (CGPoint)clampStickBase:(CGPoint)p {
    CGRect b = [self layoutBounds];
    const CGFloat m = _stickBaseR + 14; // keep the drawn ring on-screen
    return CGPointMake(MAX(b.origin.x + m, MIN(CGRectGetMaxX(b) - m, p.x)),
                       MAX(b.origin.y + m, MIN(CGRectGetMaxY(b) - m, p.y)));
}

// THE UNIFIED DEFAULT LAYOUT (Ghostship docs/PORTING-DELTAS.md, device-tuned
// by the user 2026-07-21, identical in every sibling). Edge-relative points
// against the view's FULL bounds, exactly as Lighthouse's buttonRects: does.
//
// Round 16: the four D-pad chips this port had added are GONE. The user: "there
// have been no dpad buttons on any port ... dpad doesn't do anything on paper
// mario". The eleven chips below are now literally the sibling table. The
// PB_BTN_D* bit definitions stay — the merge path still publishes whatever a
// physical controller or a remapped chip sets — but nothing draws or hits them.
- (NSArray<NSValue*>*)buttonRects:(PBButton*)outButtons count:(int*)outCount {
    CGRect b = [self layoutBounds];
    const CGFloat maxX = CGRectGetMaxX(b), maxY = CGRectGetMaxY(b);
    // File-static and built once. NOT locals: this file is compiled with ARC
    // (the CMake overlay sets -fobjc-arc), so `UIColor* c = [UIColor
    // colorWithRed:...]` is a __strong local that ARC RELEASES at the end of
    // this method — and the struct hands the pointer back to the caller.
    // Measured, the first run of this layer: a SIGSEGV inside drawRect: the
    // moment it touched a colour, immediately after the overlay's own frame
    // log. Statics are immortal and shared, which is what a palette wants.
    static UIColor *cYellow, *cBlue, *cGreen, *cGray, *cRed;
    static dispatch_once_t paletteOnce;
    dispatch_once(&paletteOnce, ^{
        cYellow = [UIColor colorWithRed:0.95 green:0.80 blue:0.15 alpha:0.55];
        cBlue = [UIColor colorWithRed:0.20 green:0.45 blue:0.95 alpha:0.6];
        cGreen = [UIColor colorWithRed:0.20 green:0.75 blue:0.35 alpha:0.6];
        cGray = [UIColor colorWithWhite:0.6 alpha:0.55];
        cRed = [UIColor colorWithRed:0.85 green:0.2 blue:0.2 alpha:0.6];
    });
    const CGFloat aX = maxX - 85, aY = maxY - 105;  // A/B cluster, bottom-right
    const CGFloat cX = maxX - 105, cY = maxY - 235; // C diamond, above it
    PBButton btns[] = {
        { CGPointMake(aX, aY), 37, cBlue, @"A", PB_BTN_A },
        { CGPointMake(aX - 92, aY - 18), 32, cGreen, @"B", PB_BTN_B },
        { CGPointMake(cX, cY - 38), 27, cYellow, @"C↑", PB_BTN_CU },
        { CGPointMake(cX, cY + 38), 27, cYellow, @"C↓", PB_BTN_CD },
        { CGPointMake(cX - 40, cY), 27, cYellow, @"C←", PB_BTN_CL },
        { CGPointMake(cX + 40, cY), 27, cYellow, @"C→", PB_BTN_CR },
        // Z below A/B, equidistant from both (unified layout): aX-68, not the
        // plain midpoint — A and B sit at different heights.
        { CGPointMake(aX - 68, maxY - 42), 34, cGray, @"Z", PB_BTN_Z },
        { CGPointMake(b.origin.x + 80, b.origin.y + 60), 34, cGray, @"L", PB_BTN_L },
        { CGPointMake(maxX - 80, b.origin.y + 60), 34, cGray, @"R", PB_BTN_R },
        { CGPointMake(CGRectGetMidX(b), maxY - 45), 30, cRed, @"START", PB_BTN_START },
        { CGPointMake(CGRectGetMidX(b), b.origin.y + 55), 26, cGray, @"≡", 0 },
    };
    const int n = (int)(sizeof(btns) / sizeof(btns[0]));
    for (int i = 0; i < n; i++) {
        NSValue* ov = _layoutOverrides[PBIos_LayoutKey(btns[i].label)];
        if (ov != nil) {
            btns[i].center = [self pointFromNormalized:[ov CGPointValue]];
        } else {
            btns[i].center = [self applyLefty:btns[i].center];
        }
        btns[i].radius *= _layoutScale;
        outButtons[i] = btns[i];
    }
    *outCount = n;
    return nil;
}

- (void)drawGlyph:(NSString*)glyph at:(CGPoint)c size:(CGFloat)fontSize alpha:(CGFloat)alpha {
    NSDictionary* attrs = @{
        NSFontAttributeName : [UIFont boldSystemFontOfSize:fontSize],
        NSForegroundColorAttributeName : [UIColor colorWithWhite:1 alpha:alpha]
    };
    const CGSize sz = [glyph sizeWithAttributes:attrs];
    [glyph drawAtPoint:CGPointMake(c.x - sz.width / 2, c.y - sz.height / 2) withAttributes:attrs];
}

- (void)drawRect:(CGRect)rect {
    CGContextRef ctx = UIGraphicsGetCurrentContext();

    // The spec rule 6: UIKit placements need the view logging its own frame,
    // once per layout, because engine screenshots cannot see UIKit.
    if (!_loggedFrame && self.bounds.size.width > 1) {
        _loggedFrame = YES;
        CGRect lb = [self layoutBounds];
        PBButton lbtn[PB_MAX_BUTTONS];
        int ln = 0;
        [self buttonRects:lbtn count:&ln];
        NSMutableString* s = [NSMutableString
            stringWithFormat:@"[PaperBoatIosShell] touch overlay frame %.0fx%.0f pt at %.0f,%.0f; safe rect "
                             @"%.0f,%.0f %.0fx%.0f pt; chips:",
                             self.frame.size.width, self.frame.size.height, self.frame.origin.x, self.frame.origin.y,
                             lb.origin.x, lb.origin.y, lb.size.width, lb.size.height];
        for (int i = 0; i < ln; i++) {
            [s appendFormat:@" %@=(%.0f,%.0f)r%.0f", PBIos_LayoutKey(lbtn[i].label), lbtn[i].center.x,
                            lbtn[i].center.y, lbtn[i].radius];
        }
        CGPoint home = [self stickHomePoint];
        [s appendFormat:@" STICK=(%.0f,%.0f)halo%.0f", home.x, home.y, (double)kPBStickHaloR];
        NSLog(@"%@", s);
    }

    if (_editMode) {
        // --- Customizer: everything visible and draggable ---
        const CGPoint home = [self stickHomePoint];
        [[UIColor colorWithRed:0.4 green:0.7 blue:1.0 alpha:0.16] setFill];
        CGContextFillEllipseInRect(
            ctx, CGRectMake(home.x - kPBStickHaloR, home.y - kPBStickHaloR, kPBStickHaloR * 2, kPBStickHaloR * 2));
        [[UIColor colorWithRed:0.4 green:0.7 blue:1.0 alpha:0.5] setStroke];
        CGContextSetLineWidth(ctx, 2);
        CGContextStrokeEllipseInRect(
            ctx, CGRectMake(home.x - kPBStickHaloR, home.y - kPBStickHaloR, kPBStickHaloR * 2, kPBStickHaloR * 2));
        const CGFloat ringR = (_stickBaseR + 8) * _layoutScale;
        [[UIColor colorWithWhite:1 alpha:0.5] setStroke];
        CGContextSetLineWidth(ctx, 4);
        CGContextStrokeEllipseInRect(ctx, CGRectMake(home.x - ringR, home.y - ringR, ringR * 2, ringR * 2));
        [[UIColor colorWithWhite:1 alpha:0.35] setFill];
        const CGFloat knobR = _stickKnobR * _layoutScale;
        CGContextFillEllipseInRect(ctx, CGRectMake(home.x - knobR, home.y - knobR, knobR * 2, knobR * 2));

        PBButton ebtns[PB_MAX_BUTTONS];
        int en = 0;
        [self buttonRects:ebtns count:&en];
        for (int i = 0; i < en; i++) {
            const PBButton bt = ebtns[i];
            const BOOL hidden = [self isButtonHidden:bt.label];
            const BOOL selected =
                (_editSelected != nil && [_editSelected isEqualToString:PBIos_LayoutKey(bt.label)]);
            const CGFloat fillA = hidden ? 0.22 : 1.0, strokeA = hidden ? 0.28 : 0.7, glyphA = hidden ? 0.35 : 0.95;
            [[bt.color colorWithAlphaComponent:CGColorGetAlpha(bt.color.CGColor) * fillA] setFill];
            [[UIColor colorWithWhite:1 alpha:(selected ? 1.0 : strokeA)] setStroke];
            const CGRect r =
                CGRectMake(bt.center.x - bt.radius, bt.center.y - bt.radius, bt.radius * 2, bt.radius * 2);
            CGContextSetLineWidth(ctx, selected ? 3 : 2);
            CGContextFillEllipseInRect(ctx, r);
            CGContextStrokeEllipseInRect(ctx, r);
            [self drawGlyph:bt.label at:bt.center size:16 * _layoutScale alpha:glyphA];
            if (selected && ![bt.label isEqualToString:@"≡"]) {
                [self drawHideBadgeAt:[self hideBadgeCenterForCenter:bt.center radius:bt.radius] hidden:hidden];
            }
        }

        // Chrome strip: [reset][scale slider][save]
        const CGRect chrome = [self editChromeRect];
        [[UIColor colorWithWhite:0 alpha:0.55] setFill];
        [[UIBezierPath bezierPathWithRoundedRect:chrome cornerRadius:14] fill];
        const CGPoint reset = [self editResetCenter];
        [[UIColor colorWithRed:0.85 green:0.25 blue:0.25 alpha:0.9] setFill];
        CGContextFillEllipseInRect(ctx, CGRectMake(reset.x - 20, reset.y - 20, 40, 40));
        [self drawGlyph:@"↺" at:reset size:22 alpha:1.0];
        const CGPoint save = [self editSaveCenter];
        [[UIColor colorWithRed:0.2 green:0.7 blue:0.35 alpha:0.95] setFill];
        CGContextFillEllipseInRect(ctx, CGRectMake(save.x - 20, save.y - 20, 40, 40));
        [self drawGlyph:@"✓" at:save size:22 alpha:1.0];
        const CGRect s = [self editSliderRect];
        [[UIColor colorWithWhite:1 alpha:0.3] setFill];
        [[UIBezierPath bezierPathWithRoundedRect:s cornerRadius:4] fill];
        const CGFloat frac = (_layoutScale - 0.7) / (1.4 - 0.7);
        const CGFloat thumbX = CGRectGetMinX(s) + frac * CGRectGetWidth(s);
        [[UIColor whiteColor] setFill];
        CGContextFillEllipseInRect(ctx, CGRectMake(thumbX - 10, CGRectGetMidY(s) - 10, 20, 20));
        [self drawGlyph:[NSString stringWithFormat:@"%d%%", (int)round(_layoutScale * 100)]
                     at:CGPointMake(CGRectGetMidX(chrome), CGRectGetMinY(chrome) - 12)
                   size:13
                  alpha:0.9];
        return;
    }

    if (_layerOff || _popupOpen || _menuOpen) {
        // The layer is off, a modal owns the screen, or the menu is up: draw
        // nothing at all. Unlike the SoH port there is no restore dot — this
        // port's menu has its own close control and overlay 0016 already gives
        // the menu real touch scrolling, so the overlay must stay out of it.
        return;
    }

    if (_controllerMode) {
        // A physical pad drives the game: only the ≡ chip remains, because it
        // is the only touch route into the menu.
        PBButton btns[PB_MAX_BUTTONS];
        int n = 0;
        [self buttonRects:btns count:&n];
        for (int i = 0; i < n; i++) {
            if (![btns[i].label isEqualToString:@"≡"]) {
                continue;
            }
            [btns[i].color setFill];
            CGContextFillEllipseInRect(ctx, CGRectMake(btns[i].center.x - btns[i].radius,
                                                       btns[i].center.y - btns[i].radius, btns[i].radius * 2,
                                                       btns[i].radius * 2));
            [self drawGlyph:@"≡" at:btns[i].center size:20 alpha:0.95];
        }
        return;
    }

    // Floating left stick: drawn only while a finger holds it.
    if (_stickActive) {
        CGContextSetLineWidth(ctx, 4);
        [[UIColor colorWithWhite:1 alpha:0.35] setStroke];
        const CGFloat ringR = (_stickBaseR + 8) * _layoutScale;
        CGContextStrokeEllipseInRect(ctx,
                                     CGRectMake(_stickBase.x - ringR, _stickBase.y - ringR, ringR * 2, ringR * 2));
        [[UIColor colorWithWhite:1 alpha:0.28] setFill];
        const CGFloat knobR = _stickKnobR * _layoutScale;
        CGContextFillEllipseInRect(ctx,
                                   CGRectMake(_stickKnob.x - knobR, _stickKnob.y - knobR, knobR * 2, knobR * 2));
    }

    PBButton btns[PB_MAX_BUTTONS];
    int n = 0;
    [self buttonRects:btns count:&n];
    for (int i = 0; i < n; i++) {
        const PBButton bt = btns[i];
        const BOOL isZ = [bt.label isEqualToString:@"Z"];
        if ([self isButtonHidden:bt.label]) {
            continue;
        }
        if (isZ && _zHeld) {
            [[UIColor colorWithRed:0.25 green:0.45 blue:0.8 alpha:0.7] setFill];
            [[UIColor colorWithWhite:1 alpha:0.7] setStroke];
        } else {
            [bt.color setFill];
            [[UIColor colorWithWhite:1 alpha:0.5] setStroke];
        }
        const CGRect r = CGRectMake(bt.center.x - bt.radius, bt.center.y - bt.radius, bt.radius * 2, bt.radius * 2);
        CGContextSetLineWidth(ctx, 3);
        CGContextFillEllipseInRect(ctx, r);
        CGContextStrokeEllipseInRect(ctx, r);
        // Labels only where the glyph is not obvious: L/R/Z/≡, as on every
        // sibling.
        const BOOL labeled = isZ || [bt.label isEqualToString:@"L"] || [bt.label isEqualToString:@"R"] ||
                             [bt.label isEqualToString:@"≡"];
        if (labeled) {
            [self drawGlyph:bt.label at:bt.center size:(bt.radius < 26 ? 16 : 20) alpha:0.95];
        }
    }
}

// Pass-through: only the stick zone and the button circles intercept touches;
// everywhere else falls through to SDL's view (game taps, the ImGui menu).
//
// TRAP (inverted from the SoH port): while the MENU or a POPUP is open this
// must return NO. Overlay 0016 reads menu swipes off ImGui's own synthesised
// mouse, which only exists if SDL sees the touch — an overlay that swallowed
// them would kill menu scrolling and the Torch first-run prompts alike.
- (BOOL)pointInside:(CGPoint)point withEvent:(UIEvent*)event {
    if (_editMode) {
        return YES; // the customizer owns the screen
    }
    if (_layerOff || _popupOpen || _menuOpen) {
        return NO;
    }
    if (_controllerMode) {
        PBButton btns[PB_MAX_BUTTONS];
        int n = 0;
        [self buttonRects:btns count:&n];
        for (int i = 0; i < n; i++) {
            if ([btns[i].label isEqualToString:@"≡"]) {
                return hypot(point.x - btns[i].center.x, point.y - btns[i].center.y) <= btns[i].radius * 1.35;
            }
        }
        return NO;
    }
    if ([self hitButton:point] >= 0) {
        return YES;
    }
    return [self pointInStickRegion:point];
}

- (void)touchesBegan:(NSSet<UITouch*>*)touches withEvent:(UIEvent*)event {
    if (_editMode) {
        for (UITouch* t in touches) {
            [self editBegan:[t locationInView:self]];
            break;
        }
        return;
    }
    if (_layerOff || _popupOpen || _menuOpen) {
        return;
    }
    if (_controllerMode) {
        // pointInside already vetted this as a ≡ tap.
        [self applyButton:@"≡" down:YES];
        return;
    }
    for (UITouch* t in touches) {
        const CGPoint p = [t locationInView:self];
        const int idx = [self hitButton:p];
        if (idx >= 0) {
            _touchButtons[[NSValue valueWithPointer:(__bridge const void*)t]] = @(idx);
            PBButton btns[PB_MAX_BUTTONS];
            int n = 0;
            [self buttonRects:btns count:&n];
            [self hapticTap];
            [self applyButton:btns[idx].label down:YES];
        } else if (!_stickActive && [self pointInStickRegion:p]) {
            [self hapticTap];
            _stickActive = YES;
            _stickTouch = t;
            _stickBase = [self clampStickBase:p];
            _stickKnob = _stickBase;
            [self updateStickAxesFromKnob];
            [self setNeedsDisplay];
        }
    }
}

- (void)touchesMoved:(NSSet<UITouch*>*)touches withEvent:(UIEvent*)event {
    if (_editMode) {
        for (UITouch* t in touches) {
            [self editMoved:[t locationInView:self]];
            break;
        }
        return;
    }
    if (_layerOff || _popupOpen || _menuOpen) {
        return;
    }
    // Button slide-across: a finger that slides from one button onto a
    // DIFFERENT one transfers the press (release old, press new) — the Z->A
    // slide class of inputs consoles always had. Sliding through the gap
    // KEEPS the current button held; empty space never stores -1.
    if (_touchButtons.count > 0) {
        PBButton sbtns[PB_MAX_BUTTONS];
        int sn = 0;
        [self buttonRects:sbtns count:&sn];
        for (UITouch* t in touches) {
            NSValue* key = [NSValue valueWithPointer:(__bridge const void*)t];
            NSNumber* boundIdx = _touchButtons[key];
            if (boundIdx == nil) {
                continue; // stick touch — handled below
            }
            const int boundI = boundIdx.intValue;
            const int nowIdx = [self hitButton:[t locationInView:self]];
            if (nowIdx >= 0 && nowIdx != boundI) {
                _touchButtons[key] = @(nowIdx);
                if (boundI >= 0 && boundI < sn) {
                    [self applyButton:sbtns[boundI].label down:NO];
                }
                [self applyButton:sbtns[nowIdx].label down:YES];
            }
        }
    }
    if (!_stickActive) {
        return;
    }
    for (UITouch* t in touches) {
        if (t != _stickTouch) {
            continue;
        }
        const CGPoint p = [t locationInView:self];
        CGFloat dx = p.x - _stickBase.x, dy = p.y - _stickBase.y;
        const CGFloat d = hypot(dx, dy);
        if (d > _stickBaseR) {
            dx = dx / d * _stickBaseR;
            dy = dy / d * _stickBaseR;
        }
        const CGPoint knob = CGPointMake(_stickBase.x + dx, _stickBase.y + dy);
        if (hypot(knob.x - _stickKnob.x, knob.y - _stickKnob.y) < 1.0) {
            continue; // sub-point jitter
        }
        _stickKnob = knob;
        [self updateStickAxesFromKnob];
        [self setNeedsDisplay];
    }
}

- (void)touchesEnded:(NSSet<UITouch*>*)touches withEvent:(UIEvent*)event {
    if (_editMode) {
        [self editEnded];
        return;
    }
    PBButton btns[PB_MAX_BUTTONS];
    int n = 0;
    [self buttonRects:btns count:&n];
    for (UITouch* t in touches) {
        NSValue* key = [NSValue valueWithPointer:(__bridge const void*)t];
        NSNumber* idx = _touchButtons[key];
        if (idx != nil) {
            [_touchButtons removeObjectForKey:key];
            const int i = idx.intValue;
            if (i >= 0 && i < n) {
                [self applyButton:btns[i].label down:NO];
            }
        }
        if (t == _stickTouch) {
            _stickActive = NO;
            _stickTouch = nil;
            _stickKnob = _stickBase;
            atomic_store(&gPBPadStickX, 0);
            atomic_store(&gPBPadStickY, 0);
            [self setNeedsDisplay];
        }
    }
}

- (void)touchesCancelled:(NSSet<UITouch*>*)touches withEvent:(UIEvent*)event {
    if (_editMode) {
        [self editEnded];
        return;
    }
    [self touchesEnded:touches withEvent:event];
}

@end

// --- Installation -----------------------------------------------------------
//
// The overlay is a subview of the SDL window's root view, with flexible
// autoresizing so it tracks every scene resize. It must NOT be installed while
// the Torch first-run modals are on screen (it would eat their taps and the
// stick halo overlaps the prompts), so installation waits for pm64.o2r — the
// same gate the siblings use — or for an engine that is up with no popup.

static PBIosTouchOverlay* gPBTouchOverlay = nil;

static BOOL PBIos_DocumentsHasArchive(void) {
    const char* home = getenv("HOME");
    if (home == NULL) {
        return NO;
    }
    NSString* docs = [NSString stringWithFormat:@"%s/Documents", home];
    return [NSFileManager.defaultManager fileExistsAtPath:[docs stringByAppendingPathComponent:@"pm64.o2r"]];
}

static void PBIos_InstallTouchOverlayIn(UIWindow* w) {
    if (w == nil || w == gPBSafeProbe || w.windowScene == nil) {
        return;
    }
    UIView* host = w.rootViewController.view ?: w;
    for (UIView* v in host.subviews) {
        if ([v isKindOfClass:PBIosTouchOverlay.class]) {
            return; // already installed
        }
    }
    PBIosTouchOverlay* overlay = [[PBIosTouchOverlay alloc] initWithFrame:host.bounds];
    overlay.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
    [host addSubview:overlay];
    gPBTouchOverlay = overlay;
    NSLog(@"[PaperBoatIosShell] touch overlay installed on %@ (host %.0fx%.0f pt)", w, host.bounds.size.width,
          host.bounds.size.height);
}

static void PBIos_InstallTouchOverlayWhenReady(int attempt) {
    if (gPBTouchOverlay != nil && gPBTouchOverlay.superview != nil) {
        return;
    }
    const BOOL ready = PBIos_DocumentsHasArchive() || (PBIos_EngineReady() && !PBIos_IsPopupOpen());
    if (ready) {
        UIWindowScene* scene = PBIos_ActiveWindowScene();
        for (UIWindow* w in scene.windows) {
            PBIos_InstallTouchOverlayIn(w);
        }
        if (gPBTouchOverlay != nil) {
            return;
        }
    }
    if (attempt < 1200) { // up to ~20 minutes of onboarding time
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.0 * NSEC_PER_SEC)), dispatch_get_main_queue(),
                       ^{ PBIos_InstallTouchOverlayWhenReady(attempt + 1); });
    }
}

static void PBIos_InstallTouchOverlay(void) {
    static BOOL scheduled = NO;
    if (scheduled) {
        // Idempotent re-assert: a scene reconnect gives a new window and the
        // old overlay went with the old one.
        dispatch_async(dispatch_get_main_queue(), ^{ PBIos_InstallTouchOverlayWhenReady(0); });
        return;
    }
    scheduled = YES;
    dispatch_async(dispatch_get_main_queue(), ^{ PBIos_InstallTouchOverlayWhenReady(0); });
}

// The bridge's `touch` verb reaches the overlay's probe (main thread only).
static NSString* PBIos_TouchProbe(NSArray<NSString*>* args) {
    if (gPBTouchOverlay == nil) {
        return @"err touch overlay not installed yet";
    }
    return [gPBTouchOverlay layoutProbe:args];
}


// ---------------------------------------------------------------------------
// The graft point. UIKit_ShowWindow() calls -[UIWindow makeKeyAndVisible] on a
// window it created with initWithFrame:, i.e. with no scene. Intercept it.
// ---------------------------------------------------------------------------
@interface UIWindow (PaperBoatSceneGraft)
@end

@implementation UIWindow (PaperBoatSceneGraft)

+ (void)load {
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        Method orig = class_getInstanceMethod(UIWindow.class, @selector(makeKeyAndVisible));
        Method mine = class_getInstanceMethod(UIWindow.class, @selector(pbios_makeKeyAndVisible));
        // Loud, not silent: a failed swizzle means a black screen later, and
        // "black screen" is the most expensive symptom to debug on this port.
        NSAssert(orig != NULL && mine != NULL, @"PaperBoatIosShell: makeKeyAndVisible swizzle failed");
        method_exchangeImplementations(orig, mine);
        NSLog(@"[PaperBoatIosShell] UIWindow scene graft installed");
        PBIos_LogBuildStamp();

        // Crash capture FIRST and from +load: this is the earliest code the
        // port runs — before main(), before the scene, before any engine
        // code — which is what "early enough to catch a crash in scene setup"
        // means. (Round 1's UIKit scene trap died before any handler existed;
        // one installed here would have caught it.)
        PBIos_InstallCrashHandler();
        PBIos_InstallLifecycleObservers();
        // The bridge does not listen unless gated in (env / Documents file);
        // starting it here rather than at scene time means a launch that dies
        // during scene setup can still be interrogated.
        PBIos_StartConsoleBridge(NO);
        PBIos_WatchRemoteConsoleCVar();
        // Deep links: take the URL where SDL's delegate would otherwise turn it
        // into a dropped file (see PBIos_InstallOpenURLHook).
        PBIos_InstallOpenURLHook();
        // UserDefaults is readable this early; the link it may hold is queued,
        // not executed, so nothing touches the engine before it exists.
        PBIos_ConsumePendingIntent();
    });
}

- (void)pbios_makeKeyAndVisible {
    PBIos_AdoptWindow(self);
    [self pbios_makeKeyAndVisible]; // swizzled: this is the original
    // SDL's window is on screen now: schedule the touch overlay. It waits for
    // pm64.o2r (or an engine with no popup) before it actually appears, so the
    // Torch first-run modals stay tappable.
    PBIos_InstallTouchOverlay();
}

@end
