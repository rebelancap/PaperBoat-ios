#!/usr/bin/env bash
# Install + launch PaperBoat on THIS session's simulator (lane 2 — iPhone Air,
# iOS 27.0), drive upstream's first-run flow, and assert that what ends up on
# screen is a real rendered game frame — the TITLE SCREEN — and not a modal
# dialog or the springboard.
#
# Usage: scripts/run-sim.sh [--keep] [--fresh] [--no-seed] [screenshot-path]
#   --keep      leave the app RUNNING at the end (default: terminate cleanly)
#   --fresh     uninstall first, so the container starts empty (exercises the
#               three-prompt first-run flow end to end)
#   --no-seed   do not pre-seed pm64.o2r even if work/sim-seed/ has one
#   PAPERBOAT_SIM_TIMEOUT=<seconds>   how long to poll for the title (default 480)
#
# WHAT ROUND 2 GOT WRONG, AND WHY THIS SCRIPT LOOKS LIKE THIS
# -----------------------------------------------------------
# 1. The old settle loop waited for pm64.o2r to appear and then screenshotted.
#    But upstream's iOS first run is THREE in-engine modal prompts (No O2R →
#    Generate? / ROMs found → Generate? / Processed → Run PaperBoat?), and
#    nothing answers them on their own. The run "passed" on a picture of a
#    dialog: the frame was not black and differed from the springboard, so
#    every check said yes.
# 2. The old script ended with `kill "$LAUNCH_PID"`, which killed the console
#    reader AND the app — so the boot it had just proved was gone by the time
#    anyone looked.
#
# So: taps are real taps (idb), the pass condition is the title screen, and the
# teardown is explicit.
#
# THE TWO WAYS PAST THE PROMPTS
#   - Seeding (default when work/sim-seed/pm64.o2r exists): copy an already
#     extracted archive into Documents before launch. AnyRomArchiveExists() is
#     then true and no prompt is ever shown. The seed is a DEVICE-extracted
#     pm64.o2r (see docs/upstream-ios-audit.md); refresh it with --fresh
#     --no-seed and copy the container's copy back out.
#   - Tapping (--no-seed, or no seed present): `idb ui tap` in the device's
#     PORTRAIT point space, which is NOT the app's landscape scene:
#         x_portrait = 420 - y_landscape      y_portrait = x_landscape
#     (iPhone Air, 912x420 pt landscape scene, native scale 3.0 — a landscape
#     coordinate lands nowhere and looks exactly like an app ignoring touch.)
#     One coordinate per prompt (they overlap in a region ~2 pt wide, so a
#     single shared point misses); `xcrun simctl` cannot tap at all.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/build-sim/Release-iphonesimulator/Paperboat.app"
# Lane 2 (spec). Never `simctl create`; never boot another lane's device.
UDID="${PAPERBOAT_SIM_UDID:-45A5059C-8751-4FC5-9BB2-A3EF6FFCCC22}"   # iPhone Air, iOS 27.0
BUNDLE_ID="${PAPERBOAT_BUNDLE_ID:-com.rebelancap.paperboat}"         # ours, overlay 0005
ROM="$ROOT/work/baserom.us.z64"
SEED="$ROOT/work/sim-seed/pm64.o2r"
TITLE_REF="$ROOT/docs/screenshots/sim-title.png"
LOG="$ROOT/work/logs/sim-run.log"

KEEP=0 FRESH=0 NOSEED=0 SHOT=""
for a in "$@"; do
    case "$a" in
        --keep) KEEP=1 ;;
        --fresh) FRESH=1 ;;
        --no-seed) NOSEED=1 ;;
        -*) echo "unknown flag: $a" >&2; exit 2 ;;
        *) SHOT="$a" ;;
    esac
done
SHOT="${SHOT:-$ROOT/artifacts/sim/boot-$(date -u +%Y%m%d-%H%M%S).png}"

die() { printf '\033[31mFATAL:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

[ -d "$APP" ] || die "no app at $APP — run scripts/build-sim.sh"
[ -f "$ROM" ] || die "no ROM at $ROM (the design notes D1)"
[ -f "$TITLE_REF" ] || die "no title reference at $TITLE_REF"
xcrun simctl list devices | grep -q "$UDID" || die "simulator $UDID not found (lane 2, iPhone Air)"
command -v idb >/dev/null || die "idb not found (brew install idb-companion && pip install fb-idb) — simctl cannot tap"
mkdir -p "$(dirname "$SHOT")" "$(dirname "$LOG")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

info "booting $UDID (idempotent)"
xcrun simctl bootstatus "$UDID" -b >/dev/null

xcrun simctl terminate "$UDID" "$BUNDLE_ID" >/dev/null 2>&1 || true
if [ "$FRESH" = 1 ]; then
    info "--fresh: uninstalling $BUNDLE_ID (container and all user data go with it)"
    xcrun simctl uninstall "$UDID" "$BUNDLE_ID" >/dev/null 2>&1 || true
fi

info "installing $APP"
xcrun simctl install "$UDID" "$APP"

# The version the app will report, straight from the bundle we just installed.
# A run that quietly tests a stale build is the most expensive kind.
PB_VERSION="$(plutil -extract CFBundleShortVersionString raw -o - "$APP/Info.plist")"
PB_BUILD="$(plutil -extract CFBundleVersion raw -o - "$APP/Info.plist")"
info "app: $BUNDLE_ID $PB_VERSION (build $PB_BUILD)"

DATA="$(xcrun simctl get_app_container "$UDID" "$BUNDLE_ID" data)"
mkdir -p "$DATA/Documents"

# Upstream has no file picker on iOS: GameExtractor::SelectGameFromUI() scans
# GetPathRelativeToAppDirectory(), which on iOS is $HOME/Documents inside the
# container. Files-app drop-in is the user's route; this is the same place by
# another door.
cp "$ROM" "$DATA/Documents/baserom.us.z64"
info "staged ROM: $DATA/Documents/baserom.us.z64"

SEEDED=0
if [ "$NOSEED" = 0 ] && [ ! -f "$DATA/Documents/pm64.o2r" ] && [ -f "$SEED" ]; then
    cp "$SEED" "$DATA/Documents/pm64.o2r"
    [ -f "$ROOT/work/sim-seed/torch.hash.yml" ] && cp "$ROOT/work/sim-seed/torch.hash.yml" "$DATA/Documents/"
    SEEDED=1
    info "seeded pm64.o2r from work/sim-seed/ — the first-run prompts will not appear"
fi
[ -f "$DATA/Documents/pm64.o2r" ] && info "pm64.o2r present: $(du -h "$DATA/Documents/pm64.o2r" | cut -f1)"

# Baseline: the springboard with the app NOT running. "The frame is not black"
# proves nothing on its own — the home screen passes that test easily.
BASE="$WORK/springboard.png"
xcrun simctl io "$UDID" screenshot "$BASE" >/dev/null 2>&1

# --console-pty puts the app's stdout/stderr (spdlog included) on our terminal.
# Backgrounded in a subshell so that nothing in this script is tempted to kill
# the app by killing the reader (round 2's bug).
# The console bridge (:8771, overlay 0006) is compiled in for sim builds but
# does not LISTEN unless it is asked to. SIMCTL_CHILD_* is how simctl passes an
# environment variable through to the launched app, and a sim run is exactly the
# case the bridge exists for: `nc localhost 8771` from this Mac reaches it,
# because the simulator shares the host's loopback.
# THE METAL API VALIDATION LAYER IS ON BY DEFAULT (round 20). The 0.0.0.14
# device crash - an ObjC exception at DrawTriangles's autorelease-pool drain,
# "Command encoder released without endEncoding" (overlay 0033 rev 3, M-032 §2)
# - never asserted on the simulator or the macOS oracle without it, and the
# validation layer catches it on the first frame that hits the path. The cost is
# some CPU per encoder, which matters to none of the counts this project reads
# off a simulator (the sim GPU prices a frame at 0.2 ms either way).
# PAPERBOAT_METAL_VALIDATION=0 turns it off.
#
# MTL_DEBUG_LAYER_ERROR_MODE=nslog IS NOT OPTIONAL HERE. In its default (assert)
# mode the layer kills this app in the first frame on an unrelated complaint -
# `Depth Clip Mode is not supported on this device`, i.e. upstream LUS's
# unconditional setDepthClipMode(Clamp), which the simulator's GPU family does
# not advertise. In nslog mode that becomes one line per encoder (grep it out)
# and every other validation failure is still reported, which is all we need:
# the encoder-lifetime bug shows up as "Command encoder released without
# endEncoding" in this log.
MTLVAL="${PAPERBOAT_METAL_VALIDATION:-1}"
if [ "$MTLVAL" != "0" ]; then
    info "Metal API validation layer ON (PAPERBOAT_METAL_VALIDATION=0 to disable)"
fi
info "launching $BUNDLE_ID (log: $LOG, console bridge gated in on :8771)"
if [ "$MTLVAL" != "0" ]; then
    ( SIMCTL_CHILD_PAPERBOAT_CONSOLE=1 SIMCTL_CHILD_METAL_DEVICE_WRAPPER_TYPE=1 \
      SIMCTL_CHILD_MTL_DEBUG_LAYER=1 SIMCTL_CHILD_MTL_DEBUG_LAYER_ERROR_MODE=nslog \
      xcrun simctl launch --console-pty "$UDID" "$BUNDLE_ID" >"$LOG" 2>&1 & )
else
    ( SIMCTL_CHILD_PAPERBOAT_CONSOLE=1 xcrun simctl launch --console-pty "$UDID" "$BUNDLE_ID" >"$LOG" 2>&1 & )
fi

# The three prompts' Yes buttons, in PORTRAIT point space, in the order they
# appear: "No O2R Files", "ROMs found", "Run PaperBoat". Measured 2026-09-16
# off 2736x1260 screenshots of upstream 1.0.0 + overlay 0001-0005 (landscape pt
# = px/3; portrait x = 420 - y_landscape, y = x_landscape).
#
# They are NOT interchangeable: the three buttons overlap in a region about
# 2 pt wide, and a single shared coordinate missed every prompt on the first
# unattended run. All three are tapped on every dialog (see below).
YES_TAPS=("202 409" "192 413" "202 400")

TIMEOUT="${PAPERBOAT_SIM_TIMEOUT:-480}"
info "polling for the title screen (up to ${TIMEOUT}s)"
DEADLINE=$((SECONDS + TIMEOUT))
BEST=-1 BESTFRAME="" ROUND=0 PASS=0 N=0
while [ $SECONDS -lt $DEADLINE ]; do
    N=$((N + 1))
    F="$WORK/poll-$N.png"
    xcrun simctl io "$UDID" screenshot "$F" >/dev/null 2>&1 || true
    [ -s "$F" ] || { sleep 5; continue; }

    # classify prints: <kind> <title-score> <unique-colours>
    read -r KIND SCORE UNIQ <<<"$(python3 "$ROOT/scripts/classify-sim-frame.py" "$F" "$TITLE_REF" "$BASE")"
    printf '    t+%03ds  %-11s title=%s colours=%s\n' "$((SECONDS))" "$KIND" "$SCORE" "$UNIQ"

    if awk -v a="$SCORE" -v b="$BEST" 'BEGIN{exit !(a>b)}'; then
        BEST="$SCORE"; BESTFRAME="$F"
    fi

    case "$KIND" in
        modal)
            # An in-engine dialog. Upstream's first run is not a fixed three
            # taps: between "ROMs found" and "Run PaperBoat?" there is also an
            # extraction progress window, which looks exactly like a prompt to
            # this classifier. So do not try to track WHICH prompt is up —
            # tap every known Yes position in turn and let the wrong ones miss.
            #
            # Missing is safe: the three Yes coordinates are all well clear of
            # every prompt's "No" button (checked against all three layouts),
            # so a stray tap lands on dead dialog background.
            ROUND=$((ROUND + 1))
            if [ "$ROUND" -gt 8 ]; then
                cp "$F" "$ROOT/artifacts/sim/modal-stuck.png"
                die "still on a dialog after $((ROUND - 1)) answer rounds — the first-run flow changed (artifacts/sim/modal-stuck.png)"
            fi
            info "dialog on screen — answering Yes (round $ROUND): ${YES_TAPS[*]}"
            for yt in "${YES_TAPS[@]}"; do
                read -r TX TY <<<"$yt"
                idb ui tap --udid "$UDID" "$TX" "$TY" >/dev/null 2>&1 \
                    || die "idb ui tap failed (is idb_companion able to reach $UDID?)"
                sleep 2
            done
            sleep 8
            continue
            ;;
        title)
            PASS=1
            cp "$F" "$SHOT"
            info "TITLE SCREEN at t+${SECONDS}s (match $SCORE vs docs/screenshots/sim-title.png)"
            break
            ;;
    esac
    sleep 10
done

# The process must still be there. A crash five seconds after a good frame is
# still a failed run.
ALIVE=0
if (set +o pipefail; xcrun simctl spawn "$UDID" launchctl list 2>/dev/null \
        | grep -q "UIKitApplication:$BUNDLE_ID"); then
    ALIVE=1
fi

# Settings persistence is what overlay 0004 bought; a run that silently goes
# back to failing every save should not read as green.
CFG="$DATA/Documents/paperboat.cfg.json"
CFGFAIL="$(grep -c 'Could not open' "$LOG" 2>/dev/null || true)"
if [ -f "$CFG" ]; then
    info "config: $(wc -c < "$CFG" | tr -d ' ') bytes at Documents/paperboat.cfg.json"
else
    printf '\033[31m    WARNING:\033[0m no paperboat.cfg.json — settings are not being saved\n'
fi
[ "${CFGFAIL:-0}" = "0" ] || printf '\033[31m    WARNING:\033[0m %s config-save failures in the log (overlay 0004 regressed?)\n' "$CFGFAIL"

if [ "$PASS" != 1 ]; then
    [ -n "$BESTFRAME" ] && cp "$BESTFRAME" "$SHOT"
    printf '\033[31m    VERDICT: NO TITLE SCREEN\033[0m — best match %s (need 0.90); alive=%s; last frame: %s\n' \
        "$BEST" "$ALIVE" "$SHOT" >&2
    [ "$KEEP" = 1 ] || xcrun simctl terminate "$UDID" "$BUNDLE_ID" >/dev/null 2>&1 || true
    exit 1
fi
[ "$ALIVE" = 1 ] || die "the app reached the title screen and then died (check for a crash .ips)"

info "screenshot: $SHOT"
info "VERDICT: $BUNDLE_ID $PB_VERSION (build $PB_BUILD) is running and showing the title screen"

if [ "$KEEP" = 1 ]; then
    info "--keep: leaving the app RUNNING on $UDID (remember the lane, and shut the device down when done)"
else
    xcrun simctl terminate "$UDID" "$BUNDLE_ID" >/dev/null 2>&1 || true
    info "app terminated (the device is still booted — shut it down at end of session)"
fi
info "done — log at $LOG"
