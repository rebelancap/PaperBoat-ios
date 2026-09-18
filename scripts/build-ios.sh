#!/usr/bin/env bash
# Build PaperBoat's iOS target for a physical DEVICE (arm64, PLATFORM=OS64).
#
# The sibling of scripts/build-sim.sh, and deliberately its mirror image: same
# overlay-first discipline, same -D cache variables, same post-build assertions.
# Only three things differ — the PLATFORM slice, the output dir (build-ios/),
# and signing, which a simulator never needs and a device always does.
#
# Output: build-ios/Release-iphoneos/Paperboat.app
#
# Signing (spec "Reserved for later"): the bundle id com.rebelancap.paperboat
# is ours, and this Mac carries a WILDCARD team provisioning profile
# (a wildcard profile, valid to 2027-07-17) plus a personal "Apple Development" identity — so automatic signing covers any com.rebelancap.* id
# without registering this one first. Signing is therefore ON by default here,
# exactly as on Lighthouse. Build UNSIGNED (device-arch compile/link proof, no
# install possible) with:
#
#     PAPERBOAT_IOS_SIGNING=OFF scripts/build-ios.sh
#
# Env knobs: PAPERBOAT_IOS_SIGNING (ON|OFF), PAPERBOAT_IOS_TEAM,
#            PAPERBOAT_REMOTE_CONSOLE (ON|OFF), JOBS.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/vendor/PaperBoat"
BUILD="$ROOT/build-ios"
JOBS="${JOBS:-6}"   # spec: 6 parallel jobs max on this shared box

die() { printf '\033[31mFATAL:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

[ -d "$SRC/.git" ] || die "vendor/PaperBoat missing — run scripts/bootstrap.sh"
mkdir -p "$ROOT/work/logs"

# The overlay, before anything reads the tree. No `|| true`: apply-overlay.sh
# exits non-zero if any patch is neither appliable nor already applied.
info "applying the overlay"
"$ROOT/scripts/apply-overlay.sh"

SHELL_DIR="$ROOT/app/ios"
[ -f "$SHELL_DIR/PaperBoatIosShell.m" ] || die "missing $SHELL_DIR/PaperBoatIosShell.m"
[ -f "$SHELL_DIR/PaperBoatIosConsole.cpp" ] || die "missing $SHELL_DIR/PaperBoatIosConsole.cpp"

# Host-prefix quarantine — see build-sim.sh. Miniforge ships macOS CMake configs
# for spdlog/fmt, LUS's ios.cmake only FetchContents when its probe MISSES, and
# the cross link then pulls in a macOS dylib. Belt and braces: ignore the
# prefixes for find_package AND keep them out of PATH so no stray tool is found.
IGNORE_PREFIXES="$HOME/Miniforge3;/opt/homebrew;/usr/local"
PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -v "^$HOME/Miniforge3" | paste -sd: -)"
export PATH

PB_VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
PB_BUILD="$(date -u +%Y%m%d%H%M)"
[ -n "$PB_VERSION" ] || die "VERSION file is empty"

SIGNING="${PAPERBOAT_IOS_SIGNING:-ON}"
TEAM="${PAPERBOAT_IOS_TEAM:?set your Apple Developer team id}"
# Console ON by default: nearly every device build is a test build, which is
# exactly where the bridge earns its keep. build-release.sh forces it OFF and
# asserts it is genuinely gone.
CONSOLE="${PAPERBOAT_REMOTE_CONSOLE:-ON}"
info "paperboat device $PB_VERSION (build $PB_BUILD), signing: $SIGNING, console: $CONSOLE"

# Upstream's CMake reads the team out of the ENVIRONMENT
# (CMAKE_XCODE_ATTRIBUTE_DEVELOPMENT_TEAM "$ENV{IOS_DEVELOPMENT_TEAM}"), not
# from a -D. Exporting it is upstream's documented interface, not a hack.
if [ "$SIGNING" = "ON" ]; then
    export IOS_DEVELOPMENT_TEAM="$TEAM"
    BUILD_EXTRA=(-allowProvisioningUpdates)
else
    unset IOS_DEVELOPMENT_TEAM || true
    BUILD_EXTRA=()
fi

# SYNC WAVE 2 trap, inherited from Lighthouse: `xcodebuild archive` replaces the
# regular-build product with a SYMLINK into ArchiveIntermediates and then
# deletes the target, so the next ordinary build dies with
#   error: unable to create directory '.../Release-iphoneos/Paperboat.app'
# Clear it surgically — the incremental build is worth keeping.
APP="$BUILD/Release-iphoneos/Paperboat.app"
if [ -L "$APP" ]; then
    info "clearing archive-poisoned product symlink (SYNC WAVE 2 trap)"
    rm -f "$APP"
fi

info "configuring (device, arm64, iOS 16.3 floor)"
cmake --no-warn-unused-cli -S "$SRC" -B "$BUILD" -G Xcode \
    -DCMAKE_TOOLCHAIN_FILE=cmake/ios.paperboat.toolchain.cmake \
    -DPLATFORM=OS64 \
    -DIOS_SIGNING="$SIGNING" \
    -DPAPERBOAT_IOS_SHELL_DIR="$SHELL_DIR" \
    -DPAPERBOAT_REMOTE_CONSOLE="$CONSOLE" \
    "-DPAPERBOAT_IOS_VERSION=$PB_VERSION" \
    "-DPAPERBOAT_IOS_BUILD=$PB_BUILD" \
    -DCMAKE_IGNORE_PREFIX_PATH="$IGNORE_PREFIXES"

info "building Release (jobs: $JOBS)"
cmake --build "$BUILD" --config Release -- -parallelizeTargets -jobs "$JOBS" \
    ${BUILD_EXTRA[@]+"${BUILD_EXTRA[@]}"}

[ -d "$APP" ] || die "expected app at $APP"
# The POST_BUILD step copies Torch's recipes into the bundle; without them the
# on-device extraction has nothing to extract with, and that failure only shows
# up at runtime — on a phone, where it costs an install round trip to learn.
[ -f "$APP/config.yml" ] || die "$APP is missing config.yml (Torch recipes)"
[ -d "$APP/assets" ] || die "$APP is missing assets/ (Torch recipes)"
[ -f "$APP/paperboat.o2r" ] || die "$APP is missing paperboat.o2r (engine assets)"

plist_get() { plutil -extract "$1" raw -o - "$APP/Info.plist" 2>/dev/null || true; }
GOT_VERSION="$(plist_get CFBundleShortVersionString)"
GOT_BUILD="$(plist_get CFBundleVersion)"
GOT_ID="$(plist_get CFBundleIdentifier)"
GOT_NAME="$(plist_get CFBundleDisplayName)"
info "Info.plist: $GOT_ID  $GOT_NAME  $GOT_VERSION (build $GOT_BUILD)"
[ "$GOT_VERSION" = "$PB_VERSION" ] \
    || die "Info.plist CFBundleShortVersionString is '$GOT_VERSION', expected '$PB_VERSION' (VERSION file)"
[ "$GOT_BUILD" = "$PB_BUILD" ] \
    || die "Info.plist CFBundleVersion is '$GOT_BUILD', expected '$PB_BUILD'"
[ "$GOT_ID" = "com.rebelancap.paperboat" ] \
    || die "Info.plist CFBundleIdentifier is '$GOT_ID', expected com.rebelancap.paperboat (overlay 0005)"
[ "$GOT_NAME" = "PaperBoat" ] \
    || die "Info.plist CFBundleDisplayName is '$GOT_NAME', expected PaperBoat"

# Files-app visibility and orientation gating live in the same plist and are
# just as easy to lose to a template edit that never reconfigured. A device is
# the only place they matter, so this is the build that checks them.
for KEY in UIFileSharingEnabled LSSupportsOpeningDocumentsInPlace; do
    [ "$(plist_get "$KEY")" = "true" ] || die "Info.plist $KEY is not true — game data would be invisible in Files"
done
ORIENT0="$(plutil -extract UISupportedInterfaceOrientations.0 raw -o - "$APP/Info.plist" 2>/dev/null || true)"
case "$ORIENT0" in
    UIInterfaceOrientationLandscape*) ;;
    *) die "Info.plist UISupportedInterfaceOrientations[0] is '$ORIENT0', expected a landscape orientation" ;;
esac
info "plist: Files sharing on, orientations landscape-only ($ORIENT0)"

# The same two numbers by the other route: generated source compiled into the
# binary (build.c.in -> gPortBuildStamp). Disagreement means a stale build tree.
STAMP="$(strings -a "$APP/Paperboat" | grep -m1 '^PaperBoat-ios ' || true)"
[ -n "$STAMP" ] || die "no compiled-in build stamp (gPortBuildStamp) in $APP/Paperboat"
info "compiled-in stamp: $STAMP"
case "$STAMP" in
    "PaperBoat-ios $PB_VERSION (build $PB_BUILD,"*) ;;
    *) die "compiled-in stamp '$STAMP' does not match $PB_VERSION / $PB_BUILD" ;;
esac

# Deep links (overlay 0009) — the scheme has to be IN THE BUILT plist.
GOT_SCHEME="$(plutil -extract CFBundleURLTypes.0.CFBundleURLSchemes.0 raw -o - "$APP/Info.plist" 2>/dev/null || true)"
[ "$GOT_SCHEME" = "paperboat" ] \
    || die "Info.plist has no paperboat:// URL scheme (got '$GOT_SCHEME'; overlay 0009)"
info "url scheme: $GOT_SCHEME://"

# ProMotion (overlay 0030 rev 2): the iPHONE key is CADisableMinimumFrameDuration
# ON PHONE. Rev 1 shipped only the iPad key and the panel stayed at 60 with the
# engine interpolating for 120 - half-speed gameplay. Asserted in the BUILT
# bundle, because that is the only place a plist edit is proven.
for k in CADisableMinimumFrameDurationOnPhone CADisableMinimumFrameDuration; do
    GOT="$(plutil -extract "$k" raw -o - "$APP/Info.plist" 2>/dev/null || true)"
    [ "$GOT" = "true" ] || die "Info.plist is missing $k (got '$GOT'; overlay 0030 rev 2)"
done
info "promotion opt-in: both CADisableMinimumFrameDuration keys present"

# The console switch is real in both directions, asserted on the binary rather
# than trusted from the flag (a mis-wired #if is exactly what this catches).
# scripts/build-release.sh repeats this on the IPA for the OFF direction.
HITS="$(strings -a "$APP/Paperboat" | grep -c 'console bridge listening' || true)"
if [ "$CONSOLE" = "ON" ]; then
    [ "$HITS" -ge 1 ] || die "console requested ON but the bridge marker is absent from the binary"
    info "remote console: compiled IN (:8771)"
else
    [ "$HITS" = "0" ] || die "console requested OFF but the bridge is STILL in the binary ($HITS markers)"
    info "remote console: absent"
fi

# Architecture, and the signature if there is meant to be one.
lipo -info "$APP/Paperboat"
ARCHS="$(lipo -info "$APP/Paperboat")"
case "$ARCHS" in
    *arm64*) ;;
    *) die "device binary is not arm64: $ARCHS" ;;
esac
case "$ARCHS" in
    *x86_64*) die "device binary contains an x86_64 slice — this is a simulator build in build-ios/" ;;
esac
if [ "$SIGNING" = "ON" ]; then
    # sed reads all input; `head -3` would SIGPIPE codesign and exit 141 under pipefail.
    codesign -dv "$APP" 2>&1 | sed -n '1,4p'
    codesign --verify --deep --strict "$APP" || die "codesign verification failed"
    info "signed, team $TEAM"
else
    info "UNSIGNED (PAPERBOAT_IOS_SIGNING=OFF) — compile/link proof only, cannot be installed"
fi

info "built (device): $APP"
du -sh "$APP" | sed 's/^/    bundle: /'
