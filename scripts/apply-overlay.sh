#!/bin/bash
# Apply the overlay patch series to vendor/PaperBoat. Idempotent and loud:
# a patch that is neither applied nor cleanly appliable fails the build.
# Patch paths are relative to vendor/PaperBoat (-p1).
#
# Every patch header states its class (the porting notes):
#   (a) program-baseline parity, (b) upstream bug fix worth sending,
#   (c) our branding. That is what keeps the upstream-bump drill cheap here.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/vendor/PaperBoat"
PATCHES="$ROOT/overlay/patches"

[[ -d "$VENDOR/.git" ]] || { echo "FATAL: vendor missing — run scripts/bootstrap.sh" >&2; exit 1; }

shopt -s nullglob
series=("$PATCHES"/[0-9][0-9][0-9][0-9]-*.patch)
if [[ ${#series[@]} -eq 0 ]]; then
    echo "overlay: no patches"
    exit 0
fi

applied=0 skipped=0
for p in "${series[@]}"; do
    name="$(basename "$p")"
    # Forward dry-run first. --force on BOTH probes: without it, patch(1)
    # direction-guesses and exits 0 on the wrong direction (observed on the
    # SoH port: an unapplied patch passed the -R probe and was silently
    # skipped, so the build shipped without it and nothing said a word).
    if patch -p1 --forward --force --fuzz=0 --dry-run -d "$VENDOR" < "$p" > /dev/null 2>&1; then
        patch -p1 --forward --force --fuzz=0 -d "$VENDOR" < "$p" > /dev/null
        echo "overlay: applied $name"
        applied=$((applied + 1))
    elif patch -p1 -R --force --fuzz=0 --dry-run -d "$VENDOR" < "$p" > /dev/null 2>&1; then
        skipped=$((skipped + 1))
    else
        echo "FATAL: overlay patch $name neither applied nor appliable" >&2
        exit 1
    fi
done

# patch(1) leaves .orig/.rej backups behind. Harmless, but they sit in the
# vendor tree and shadow real files in greps (a sibling's symbol lookup matched
# a stale .orig once and reported the wrong verdict). Sweep them every run.
litter=$(find "$VENDOR" \( -name '*.orig' -o -name '*.rej' \) | wc -l | tr -d ' ')
if [[ "$litter" != "0" ]]; then
    find "$VENDOR" \( -name '*.orig' -o -name '*.rej' \) -delete
    echo "overlay: swept $litter patch backup file(s)"
fi

echo "overlay: $applied applied, $skipped already-applied, $((applied + skipped))/${#series[@]} total"
