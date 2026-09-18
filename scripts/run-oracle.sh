#!/usr/bin/env bash
# run-oracle.sh — run the macOS ground-truth reference build.
#
# Two non-obvious things, and they are the whole reason this script exists:
#
# 1. SHIP_HOME. libultraship's Ship::Context::GetAppDirectoryPath() honours
#    $SHIP_HOME on Apple platforms, and that directory is where the oracle keeps
#    everything the user owns: paperboat.cfg.json, saves/, mods/, logs/ and the
#    extracted pm64.o2r. Pointing it at oracle/shiphome/ keeps the oracle out of
#    ~/Library entirely, so a wipe is `rm -rf oracle/shiphome`.
#    (LUS still makes an empty ~/Library/Application Support/<basename> as a side
#    effect of reading SHIP_HOME — harmless, upstream quirk.)
#    It is an ABSOLUTE path again since overlay 0004 — see the note at the
#    bottom of this file.
# 2. The ROM. On desktop the extractor scans the app directory and SHIP_HOME for
#    *.z64 whose SHA-1 config.yml has a recipe for, so the ROM has to sit in
#    SHIP_HOME before first launch. config.yml, assets/ and paperboat.o2r are
#    found next to the binary (GetAppBundlePath() == the executable's directory
#    for a non-bundled build), which is why we also cd there.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/oracle/build"
BIN="$BUILD/Paperboat"
SHIP_HOME="$ROOT/oracle/shiphome"
ROM="$ROOT/work/baserom.us.z64"

die() { printf '\033[31mFATAL:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

[ -x "$BIN" ] || die "no oracle binary at $BIN — run scripts/build-oracle.sh"
[ -f "$BUILD/paperboat.o2r" ] || die "missing $BUILD/paperboat.o2r (engine assets; the renderer will not come up without it)"
[ -f "$BUILD/config.yml" ] || die "missing $BUILD/config.yml (Torch ROM recipes)"
[ -d "$BUILD/assets" ] || die "missing $BUILD/assets/ (Torch extraction recipes)"

mkdir -p "$SHIP_HOME"

# First launch needs the ROM; later launches only need pm64.o2r, but keeping the
# ROM there costs nothing and makes a re-extract a one-click affair.
if [ ! -f "$SHIP_HOME/baserom.us.z64" ]; then
    [ -f "$ROM" ] || die "no ROM at $ROM (see the design notes D1) and no pm64.o2r in $SHIP_HOME"
    info "Staging ROM into SHIP_HOME (first run: the game extracts pm64.o2r from it)"
    cp "$ROM" "$SHIP_HOME/baserom.us.z64"
fi

if [ ! -f "$SHIP_HOME/pm64.o2r" ]; then
    info "No pm64.o2r yet — this launch will run Torch over the ROM (minutes, with a progress bar)"
fi

info "oracle:    $BIN"
info "built:     $(date -r "$BIN" '+%Y-%m-%d %H:%M')"
info "SHIP_HOME: $SHIP_HOME"

# SHIP_HOME is the ABSOLUTE path, which is what it should always have been.
#
# Until overlay 0004 it had to be "." with the working directory inside
# oracle/shiphome, to work around an upstream bug: Engine.cpp passed an already
# absolute GetPathRelativeToAppDirectory("paperboat.cfg.json") into
# Ship::Context, which prefixes it with the app directory a second time
# (".../shiphome/.../shiphome/paperboat.cfg.json"), so every settings save
# failed with "Could not open ... to save config". A relative SHIP_HOME made
# the double prefix harmless ("././paperboat.cfg.json") — a run-script trick,
# not a fix, and useless on iOS where there is no SHIP_HOME at all.
#
# Overlay 0004 fixes it in the port (the design notes D8), so the workaround is gone.
# Verified 2026-09-16 against a vendor+overlay oracle build: absolute SHIP_HOME,
# zero "Could not open" lines in a 40 s run, and paperboat.cfg.json rewritten in
# place in oracle/shiphome/. The one leftover is LUS's stray empty
# ~/Library/Application Support/shiphome, which the relative path also used to
# dodge; harmless, and a separate LUS quirk.
#
# The working directory still moves into SHIP_HOME so that any code that falls
# back to "./" lands somewhere sane; config.yml, assets/ and paperboat.o2r are
# found next to the binary via GetAppBundlePath(), which does not depend on it.
cd "$SHIP_HOME"
export SHIP_HOME
exec "$BIN" "$@"
