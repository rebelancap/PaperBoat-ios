#!/usr/bin/env bash
# bootstrap.sh — reproduce the pinned vendor checkout from nothing.
#
# Program rule: upstream stays pristine, pinned by commit. This script is the
# ONE command that recreates vendor/ on a clean machine. Failures are loud:
# no `|| true`, and every pin is asserted after checkout.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$ROOT/vendor/PaperBoat"

# --- D0 pins (see the design notes) ---------------------------------------------
PB_REPO="https://github.com/HarbourMasters/PaperBoat.git"
# Upstream 1.0.0 (2026-09-16, "Bump project version to 1.0.0"), tip of develop
# on the day this port started. LUS is JeodC/libultraship branch lus-converge.
PB_PIN="5489baad1c08bc134ed894b96cf66c3da615deed"
LUS_PIN="7aa03b6c830b059e3ddd6ad20d3f289c5f406161"
TORCH_PIN="106f4e3056f07fe3a8758bb14a060f8423b6877f"

die() { printf '\033[31mFATAL:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

if [ ! -d "$VENDOR/.git" ]; then
  info "Cloning $PB_REPO -> vendor/PaperBoat"
  mkdir -p "$ROOT/vendor"
  git clone --recursive "$PB_REPO" "$VENDOR"
fi

info "Pinning to D0 commits"
git -C "$VENDOR" fetch --all --tags --quiet
git -C "$VENDOR" checkout --quiet "$PB_PIN"
git -C "$VENDOR" submodule update --init --recursive --quiet

assert_pin() {
  local label="$1" dir="$2" want="$3" got
  got="$(git -C "$dir" rev-parse HEAD)"
  [ "$got" = "$want" ] || die "$label pin mismatch: want $want, got $got"
  printf '    %-12s %s\n' "$label" "$got"
}
assert_pin "PaperBoat"    "$VENDOR"                        "$PB_PIN"
assert_pin "libultraship" "$VENDOR/external/libultraship"  "$LUS_PIN"
assert_pin "Torch"        "$VENDOR/external/torch"         "$TORCH_PIN"

# --- vendor purity: tracked modifications must be EXACTLY the overlay --------
DIRTY="$(git -C "$VENDOR" status --porcelain --untracked-files=no)"
if [ -n "$DIRTY" ]; then
  shopt -s nullglob
  series=("$ROOT"/overlay/patches/[0-9][0-9][0-9][0-9]-*.patch)
  [ "${#series[@]}" -gt 0 ] || die "vendor has tracked modifications but no overlay patches exist:\n$DIRTY"
  for ((i=${#series[@]}-1; i>=0; i--)); do
    patch -R --dry-run --fuzz=0 -p1 -d "$VENDOR" < "${series[$i]}" >/dev/null \
      || die "vendor is not pristine-plus-overlay: $(basename "${series[$i]}") does not reverse cleanly"
  done
  info "vendor = pristine + overlay (${#series[@]} patches reverse cleanly)"
else
  info "vendor is pristine"
fi
info "bootstrap OK"
