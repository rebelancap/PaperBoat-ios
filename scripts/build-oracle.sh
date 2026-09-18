#!/usr/bin/env bash
# build-oracle.sh — build the permanent macOS ground-truth reference (Phase 0.2).
#
# The oracle is upstream PaperBoat built natively on this Mac with the user's own
# ROM data. It is the reference for every visual/feel comparison on the iOS port,
# and it must stay green on every upstream bump.
#
# Deliberately NOT `cmake --preset ninja-release`: upstream's preset puts the
# build tree under vendor/PaperBoat/build/, and vendor/ is pristine. Same
# generator and build type, out-of-tree in oracle/build/ (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$ROOT/vendor/PaperBoat"
BUILD="$ROOT/oracle/build"
JOBS="${JOBS:-6}"   # spec: --parallel 6 max, shared box

die() { printf '\033[31mFATAL:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

[ -d "$VENDOR/.git" ] || die "vendor/PaperBoat missing — run scripts/bootstrap.sh first"
[ -f "$VENDOR/external/libultraship/CMakeLists.txt" ] || die "libultraship submodule not checked out"
command -v cmake >/dev/null || die "cmake not found (brew install cmake)"
command -v ninja >/dev/null || die "ninja not found (brew install ninja)"

# This Mac has a Miniforge (conda) prefix on PATH, and CMake searches the
# parents of PATH entries. That prefix carries its OWN fmt/spdlog/Vulkan, which
# CMake then mixes with Homebrew's headers — the build links Miniforge's
# libfmt.12.1.0 against Homebrew's fmt 12.2.0 headers and dies at link time with
# "Undefined symbols: fmt::v12::detail::allocate". Keep Miniforge out of the
# search entirely; Homebrew supplies every dependency.
CONDA_PREFIX_DIR="$HOME/Miniforge3"
PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -v "^$CONDA_PREFIX_DIR" | paste -sd: -)"
export PATH

info "Configuring (Release, Ninja) -> $BUILD"
# CMAKE_POLICY_VERSION_MINIMUM: CMake 4 rejects the vendored deps' old
# cmake_minimum_required otherwise (same trap as the sibling ports).
# CMAKE_DISABLE_FIND_PACKAGE_Vulkan: this Mac has Vulkan headers/loader in
# ~/Miniforge3 but no shaderc. LUS turns its Vulkan backend ON whenever
# find_package(Vulkan) succeeds, and gfx_vulkan.cpp includes <shaderc/shaderc.hpp>
# unconditionally -> "fatal error: 'shaderc/shaderc.hpp' file not found".
# The oracle only ever uses Metal (that is the iOS-relevant path), so the fix is
# to keep Vulkan out of the configure entirely. Upstream bug worth reporting.
cmake -S "$VENDOR" -B "$BUILD" -GNinja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
  -DCMAKE_DISABLE_FIND_PACKAGE_Vulkan=ON \
  -DCMAKE_IGNORE_PREFIX_PATH="$CONDA_PREFIX_DIR"

info "Building paperboat.o2r (engine assets: shaders, fonts, button art)"
cmake --build "$BUILD" --target GeneratePortO2R

info "Building Paperboat (--parallel $JOBS)"
cmake --build "$BUILD" --parallel "$JOBS"

BIN="$BUILD/Paperboat"
[ -x "$BIN" ] || die "expected binary not produced: $BIN"
[ -f "$BUILD/paperboat.o2r" ] || die "missing $BUILD/paperboat.o2r"
[ -f "$BUILD/config.yml" ] || die "missing $BUILD/config.yml (Torch ROM recipes)"
[ -d "$BUILD/assets" ] || die "missing $BUILD/assets/ (Torch extraction recipes)"

info "Oracle binary: $BIN ($(stat -f%z "$BIN") bytes)"
info "Upstream pin: $(git -C "$VENDOR" rev-parse --short HEAD) ($(git -C "$VENDOR" describe --tags --always))"
