#!/usr/bin/env python3
"""Overlay patch 0040 - SDL2 on iOS 27: interface orientation from the window
scene, not the status bar (port of Shipwright 0059 / Ghostship 0054).

CLASS: (b) upstream bug fix worth sending (SDL's, carried through LUS).

THE BUG. iOS 27 made -[UIApplication statusBarOrientation] a deprecated no-op
for apps linked against the iOS 27 SDK (it reports Unknown). SDL 2.32.10's
UIKit backend trusts it in four places (SDL_OnApplicationDidChangeStatusBar-
Orientation, UIKit_IsDisplayLandscape, UIKit_ComputeViewFrame, the launch-image
lookup), so an iOS-27-SDK build decides the landscape-only game is PORTRAIT:
at launch (~1 s in, the first layout pass) and on every activation (Home-and-
return, Notification/Control Center). The display modes swap to portrait and
the engine renders a portrait 2D size into a landscape drawable. This is the
"PB_SSAA in=1260x2736" portrait launch STATUS rounds 24-26 recorded as
"recurring, unexplained" (3 of 5 cold launches) - ~/dev/the porting notes "Second
iOS 27 SDK trap".

THE FIX.
1. Adds external/libultraship/cmake/dependencies/patches/sdl2-ios27-orientation.patch
   (source of truth: overlay/files/sdl2-ios27-orientation.patch). It is
   Shipwright's patch re-based onto PRISTINE SDL release-2.32.10 (Shipwright
   and Ghostship carry a visionOS-compat SDL patch underneath; PaperBoat does
   not, so two hunks in SDL_uikitvideo.{h,m} moved). Content is unchanged:
   UIKit_GetWindowScene/UIKit_GetInterfaceOrientation read the window scene's
   interfaceOrientation (Unknown or no scene -> landscape); every
   statusBarOrientation read uses it; ComputeViewFrame returns the scene's
   coordinate-space bounds unflipped when the window has a scene;
   SetupWindowData no longer swaps w/h for a window with a scene; the view
   controller re-syncs the display mode on every layout pass and at the end of
   every size transition. Every hunk is iOS-only code.
2. Hunks LUS cmake/dependencies/ios.cmake to apply it at configure time right
   after SDL2's FetchContent_MakeAvailable, through LUS's own git-patch.cmake
   (apply, or accept already-applied, else FATAL_ERROR).

The shell's landscape self-heal (app/ios/PaperBoatIosShell.m, Shipwright
a69a0f6 + fdac188) is the backstop that re-derives the view chain after an
activation; this patch removes the cause.

Generator input: the vendor tree with 0001..0039 applied and 0040 not applied
(idempotent: an already-present ios.cmake hunk is reversed in memory first).
"""
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
LUS_REL = "external/libultraship"
SDL_PATCH_SRC = ROOT / "overlay/files/sdl2-ios27-orientation.patch"
OUT = ROOT / "overlay/patches/0040-lus-sdl2-ios27-orientation.patch"


def udiff(rel_a, rel_b, a_text, b_text, new_file=False):
    with tempfile.TemporaryDirectory() as td:
        fa = pathlib.Path(td) / "a"
        fb = pathlib.Path(td) / "b"
        fa.write_text(a_text)
        fb.write_text(b_text)
        args = ["diff", "-u" + ("N" if new_file else ""), "--label", rel_a, "--label", rel_b, str(fa), str(fb)]
        r = subprocess.run(args, capture_output=True)
    assert r.returncode == 1, f"{rel_b}: diff produced no change"
    return r.stdout.decode()


chunks = []

# 1. the SDL patch file, as a new file inside LUS.
sdl_text = SDL_PATCH_SRC.read_text()
assert sdl_text.count("statusBarOrientation") >= 6, "SDL patch source looks wrong"
new_rel = f"{LUS_REL}/cmake/dependencies/patches/sdl2-ios27-orientation.patch"
assert not (VENDOR / new_rel).exists() or (VENDOR / new_rel).read_text() == sdl_text, \
    f"{new_rel} exists with different content - reverse 0040 first"
chunks.append(udiff("/dev/null", f"b/{new_rel}", "", sdl_text, new_file=True))

# 2. ios.cmake: apply it after SDL2's FetchContent_MakeAvailable.
cmake_rel = f"{LUS_REL}/cmake/dependencies/ios.cmake"
cur = (VENDOR / cmake_rel).read_text()
OLD = """    FetchContent_MakeAvailable(SDL2)
endif()

"""
NEW = """    FetchContent_MakeAvailable(SDL2)
endif()
# PAPERBOAT_IOS (overlay 0040): iOS 27 made statusBarOrientation a no-op; SDL's
# UIKit backend reads the window scene's interfaceOrientation instead (Shipwright
# 0059, re-based onto pristine release-2.32.10). git-patch.cmake without reset =
# apply, or accept already-applied, else FATAL.
if (NOT ${SDL2_FOUND})
    if (NOT sdl2_SOURCE_DIR)
        message(FATAL_ERROR "overlay 0040: sdl2_SOURCE_DIR unset; cannot apply sdl2-ios27-orientation.patch")
    endif()
    execute_process(
        COMMAND ${CMAKE_COMMAND} -Dpatch_file=${CMAKE_CURRENT_SOURCE_DIR}/cmake/dependencies/patches/sdl2-ios27-orientation.patch -Dwith_reset=FALSE -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/dependencies/git-patch.cmake
        WORKING_DIRECTORY ${sdl2_SOURCE_DIR}
        RESULT_VARIABLE sdl2_ios27_patch_ret
    )
    if (NOT sdl2_ios27_patch_ret EQUAL 0)
        message(FATAL_ERROR "overlay 0040: sdl2-ios27-orientation.patch failed to apply")
    endif()
endif()

"""
orig = cur.replace(NEW, OLD) if cur.count(NEW) == 1 else cur
n = orig.count(OLD)
assert n == 1, f"{cmake_rel}: expected 1 match of the SDL2 FetchContent block, got {n}"
chunks.append(udiff(f"a/{cmake_rel}", f"b/{cmake_rel}", orig, orig.replace(OLD, NEW)))

OUT.write_text(__doc__ + "\n" + "".join(chunks))
print(f"wrote {OUT.relative_to(ROOT)}")
