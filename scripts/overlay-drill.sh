#!/usr/bin/env bash
# Overlay integrity drill (the siblings' M-017 invariants, made runnable).
#
# Three things the whole overlay discipline rests on, checked by DOING them:
#
#   1. The series is exactly reversible: reverse every patch in reverse order
#      and vendor/ must be byte-identical to the D0 pin.
#   2. Every patch is faithfully reproduced by its generator (patch files are
#      OUTPUTS, never sources). Regenerate all of them against the now-pristine
#      tree and diff.
#   3. The series applies FROM SCRATCH in numeric order. This catches the
#      specific hazard of inserting a patch into the middle of an existing
#      series: a generator reads the CURRENT (fully patched) tree, so a new
#      patch's context can silently depend on a LATER patch's edits, and
#      everything looks fine until someone bootstraps from a clean checkout.
#
# Leaves the tree fully patched, i.e. exactly where it started.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$ROOT/vendor/PaperBoat"
PATCHES="$ROOT/overlay/patches"

# A drill that fails halfway leaves vendor/ half-reversed, and the NEXT run then
# fails somewhere else entirely for a reason unrelated to the real problem.
# Always put the tree back.
restore() {
    local rc=$?
    find "$VENDOR" \( -name '*.orig' -o -name '*.rej' \) -delete 2>/dev/null || true
    "$ROOT/scripts/apply-overlay.sh" > /dev/null 2>&1 || true
    exit "$rc"
}
trap restore EXIT

die() { printf '\033[31mFATAL:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[32m  OK\033[0m %s\n' "$*"; }

[ -d "$VENDOR/.git" ] || die "vendor missing — run scripts/bootstrap.sh"

shopt -s nullglob
series=("$PATCHES"/[0-9][0-9][0-9][0-9]-*.patch)
[ ${#series[@]} -gt 0 ] || die "no patches"
info "${#series[@]} patches in the series"

# --- 1. exact reversibility --------------------------------------------------
info "1/3  reversing the series (newest first)"
for ((i = ${#series[@]} - 1; i >= 0; i--)); do
    p="${series[$i]}"
    patch -p1 -R --force --fuzz=0 -d "$VENDOR" < "$p" > /dev/null \
        || die "reverse failed at $(basename "$p")"
done
find "$VENDOR" \( -name '*.orig' -o -name '*.rej' \) -delete

# IN-SOURCE BUILD ARTIFACT, not overlay dirt: the Xcode generator drops
# CMakeScripts/ inside the source tree for sub-projects whose binary dir points
# back at their source dir. Nothing the overlay did; a rebuild recreates it.
rm -rf "$VENDOR/CMakeScripts" "$VENDOR/external/libultraship/CMakeScripts"

DIRTY="$(git -C "$VENDOR" status --porcelain)"
[ -z "$DIRTY" ] || {
    printf '%s\n' "$DIRTY" >&2
    die "vendor is NOT byte-identical to the pin after reversing the series"
}
ok "vendor is byte-identical to the D0 pin"

# --- 2. generators reproduce their patches -----------------------------------
info "2/3  regenerating every patch from the pristine tree"
# Compare against a SNAPSHOT of what is on disk, not against git HEAD: using
# git would report every uncommitted patch as drift, i.e. it would be loudest
# during exactly the session that is adding patches. What we want to know is
# whether the generators reproduce the patch files byte-for-byte.
SNAP="$(mktemp -d)"
cp "$PATCHES"/*.patch "$SNAP/"
for g in "$ROOT"/scripts/gen-patch-*.py; do
    python3 "$g" > /dev/null || die "generator failed: $(basename "$g")"
done
# Compare *.patch only — overlay/patches/ also carries a .gitkeep, and a
# whole-directory diff would call that "drift" forever.
drift=""
for f in "$PATCHES"/*.patch; do
    b="$(basename "$f")"
    if ! cmp -s "$SNAP/$b" "$f"; then
        drift="$drift $b"
    fi
done
for f in "$SNAP"/*.patch; do
    b="$(basename "$f")"
    [ -f "$PATCHES/$b" ] || drift="$drift $b(vanished)"
done
if [ -n "$drift" ]; then
    printf 'drifted:%s\n' "$drift" >&2
    rm -rf "$SNAP"
    die "a patch differs from what its generator produces (patch files are OUTPUTS, never sources)"
fi
rm -rf "$SNAP"
ok "every patch matches its generator's output"

# --- 3. from-scratch application in numeric order ----------------------------
# The tree is pristine right now, which is exactly the bootstrap state. Apply
# forward with --fuzz=0 and no --force: a patch whose context depends on a
# LATER patch fails here and nowhere else.
info "3/3  applying the series from scratch, in order, fuzz=0"
for p in "${series[@]}"; do
    patch -p1 --forward --fuzz=0 -d "$VENDOR" < "$p" > /dev/null \
        || die "from-scratch apply failed at $(basename "$p") — its context probably \
depends on a patch numbered AFTER it (generators read the fully patched tree; that is the trap)"
done
find "$VENDOR" \( -name '*.orig' -o -name '*.rej' \) -delete
ok "the series applies cleanly from a pristine checkout"

info "drill green — tree left fully patched"
