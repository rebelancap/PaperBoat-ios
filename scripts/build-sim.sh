#!/usr/bin/env bash
# Build PaperBoat's iOS target for the iOS SIMULATOR (arm64).
#
# vendor/PaperBoat is upstream + the overlay: scripts/apply-overlay.sh runs
# FIRST, every time, and a patch that neither applies nor is already applied
# fails the build loudly. Everything else below is a -D cache variable, which
# is how a vendor tree stays free of machine-specific edits. See
# docs/upstream-ios-audit.md for what each flag is compensating for.
#
# Output: build-sim/Release-iphonesimulator/Paperboat.app
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/vendor/PaperBoat"
BUILD="$ROOT/build-sim"
JOBS="${JOBS:-6}"   # spec: 6 parallel jobs max on this shared box

die() { printf '\033[31mFATAL:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

[ -d "$SRC/.git" ] || die "vendor/PaperBoat missing — run scripts/bootstrap.sh"
mkdir -p "$ROOT/work/logs"

# The overlay, before anything reads the tree. No `|| true`: apply-overlay.sh
# exits non-zero if any patch is neither appliable nor already applied, and
# set -e turns that into a failed build rather than a silently stale one.
info "applying the overlay"
"$ROOT/scripts/apply-overlay.sh"

# The app shell that lives in THIS repo (UIScene adoption + the SDL window
# graft; overlay 0001 wires it into the iOS target). Asserted here as well as
# in CMake so the failure names the script that forgot it.
SHELL_DIR="$ROOT/app/ios"
[ -f "$SHELL_DIR/PaperBoatIosShell.m" ] || die "missing $SHELL_DIR/PaperBoatIosShell.m"
# The C++ side of the shell (overlay 0006): Console::Run and Config save behind
# a C ABI, so the shell itself can stay Objective-C.
[ -f "$SHELL_DIR/PaperBoatIosConsole.cpp" ] || die "missing $SHELL_DIR/PaperBoatIosConsole.cpp"

# Host-prefix quarantine. libultraship's cmake/dependencies/ios.cmake probes
# `find_package(spdlog QUIET)` and only fetches an iOS build when that MISSES.
# On this box Miniforge ships a macOS spdlog CMake config, so the probe HITS and
# the cross build links a macOS dylib:
#   ld: building for 'iOS-simulator', but linking in dylib
#       (~/Miniforge3/lib/libspdlog.1.17.0.dylib) built for 'macOS'
# Upstream CI never sees this (clean macos-15 runners have no such prefix), so
# it is an environment fix, not an upstream bug. Ignoring the prefixes sends
# every such probe back to FetchContent, which builds them for the simulator.
IGNORE_PREFIXES="$HOME/Miniforge3;/opt/homebrew;/usr/local"

# Upstream's documented iOS configure, with PLATFORM switched to the simulator
# slice. ios.paperboat.toolchain.cmake sets DEPLOYMENT_TARGET=16.3 BEFORE
# ios.toolchain.cmake bakes it into the -target triple; the configure hard-fails
# below 16.3 (std::format's float path needs std::to_chars).
# -G Xcode is upstream's requirement, not a preference: the target is an app
# bundle and the generator has to know how to make one.
# IOS_SIGNING=OFF gives an unsigned bundle, which is what a simulator wants.
# The port's own version, never upstream's project version (overlay 0005,
# the publishing conventions §1). VERSION is the single source of truth for the
# public string; the build number is a UTC timestamp so that every build is a
# different CFBundleVersion — iOS can skip a reinstall when both strings match.
PB_VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
PB_BUILD="$(date -u +%Y%m%d%H%M)"
[ -n "$PB_VERSION" ] || die "VERSION file is empty"
info "paperboat sim $PB_VERSION (build $PB_BUILD)"

info "configuring (simulator, arm64, iOS 16.3 floor)"
cmake --no-warn-unused-cli -S "$SRC" -B "$BUILD" -G Xcode \
    -DCMAKE_TOOLCHAIN_FILE=cmake/ios.paperboat.toolchain.cmake \
    -DPLATFORM=SIMULATORARM64 \
    -DIOS_SIGNING=OFF \
    -DPAPERBOAT_IOS_SHELL_DIR="$SHELL_DIR" \
    -DPAPERBOAT_REMOTE_CONSOLE=ON \
    "-DPAPERBOAT_IOS_VERSION=$PB_VERSION" \
    "-DPAPERBOAT_IOS_BUILD=$PB_BUILD" \
    -DCMAKE_IGNORE_PREFIX_PATH="$IGNORE_PREFIXES"

info "building Release (jobs: $JOBS)"
cmake --build "$BUILD" --config Release -- -parallelizeTargets -jobs "$JOBS"

APP="$BUILD/Release-iphonesimulator/Paperboat.app"
[ -d "$APP" ] || die "expected app at $APP"
# The POST_BUILD step copies Torch's recipes into the bundle; without them the
# on-device extraction has nothing to extract with, and that failure only shows
# up at runtime.
[ -f "$APP/config.yml" ] || die "$APP is missing config.yml (Torch recipes)"
[ -d "$APP/assets" ] || die "$APP is missing assets/ (Torch recipes)"
[ -f "$APP/paperboat.o2r" ] || die "$APP is missing paperboat.o2r (engine assets)"

# Build-stamp assertion, post-build and loud. The SoH port shipped an OTA build
# whose hub page said 9.2.3 while the IPA said 1.0.0 because nothing ever
# compared the two; this is that comparison, against the built bundle.
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
# The same two numbers by the other route: generated source compiled into the
# binary (build.c.in -> gPortBuildStamp). If these disagree with the plist, the
# build tree is stale.
STAMP="$(strings -a "$APP/Paperboat" | grep -m1 '^PaperBoat-ios ' || true)"
[ -n "$STAMP" ] || die "no compiled-in build stamp (gPortBuildStamp) in $APP/Paperboat"
info "compiled-in stamp: $STAMP"
case "$STAMP" in
    "PaperBoat-ios $PB_VERSION (build $PB_BUILD,"*) ;;
    *) die "compiled-in stamp '$STAMP' does not match $PB_VERSION / $PB_BUILD" ;;
esac

# Deep links (overlay 0009): the scheme has to be IN THE BUILT plist, not just
# in the template — a plist edit that silently did not reconfigure is exactly
# how "simctl openurl does nothing" happens.
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

lipo -info "$APP/Paperboat"
info "built (simulator): $APP"
