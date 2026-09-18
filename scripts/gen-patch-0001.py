#!/usr/bin/env python3
"""Overlay patch 0001 — UIScene lifecycle adoption for the iOS target.

CLASS: (b) upstream bug fix worth sending. This is not a PaperBoat-ios
preference: built against the iOS 26+/27 SDK, upstream's own iOS target SIGTRAPs
inside UIKit before main() for EVERY user, because SDL 2.32.10's UIKit backend
has no UIScene adoption:

    EXC_BREAKPOINT (SIGTRAP)
    0  UIKitCore  ___UIApplicationEvaluateRuntimeIssueForNoSceneLifecycleAdoption_block_invoke
    23 UIKitCore  UIApplicationMain
    24 Paperboat  SDL_UIKitRunApp

Full evidence: docs/upstream-ios-audit.md. A bare UIApplicationSceneManifest is
NOT enough — measured, same trap. UIKit wants real adoption: a
UISceneConfigurations entry naming a scene delegate class.

Two edits, both minimal on purpose (D6):

  ios/plist.in    declare UISceneConfigurations → PBIosSceneDelegate.
  CMakeLists.txt  compile the port's app shell (app/ios/PaperBoatIosShell.m,
                  passed in with -DPAPERBOAT_IOS_SHELL_DIR) into the target.
                  It implements PBIosSceneDelegate and grafts the SDL-created
                  UIWindow onto the connected scene — without that graft the
                  app clears the trap and then renders nothing, because a
                  window with windowScene == nil never appears.

The shell lives in THIS repo, not under vendor/, so an upstream bump never
merges shell code; the vendor side stays two hunks wide.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0001-paperboat-ios-uiscene-adoption.patch"


def edit(rel, old, new, count=1):
    """Return a unified diff for one asserted single-match edit."""
    src = VENDOR / rel
    orig = src.read_text()
    n = orig.count(old)
    assert n == count, f"{rel}: expected {count} match(es) of anchor, got {n}"
    text = orig.replace(old, new)
    with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
         tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
        fa.write(orig); fb.write(text); fa.flush(); fb.flush()
        r = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}",
                            fa.name, fb.name], capture_output=True)
    assert r.returncode == 1, f"{rel}: diff produced no change"
    return r.stdout.decode()


# ---------------------------------------------------------------- CMakeLists.txt
CMAKE_OLD = """    add_executable(${PROJECT_NAME}
        ${GAME_SOURCES}
        ${PORT_SOURCES}
        ${STORYBOARD_FILE}
        ${IMAGE_FILES}
        ${ICON_FILES}
    )

    set_target_properties(${PROJECT_NAME} PROPERTIES
"""

CMAKE_NEW = """    add_executable(${PROJECT_NAME}
        ${GAME_SOURCES}
        ${PORT_SOURCES}
        ${STORYBOARD_FILE}
        ${IMAGE_FILES}
        ${ICON_FILES}
    )

    # PAPERBOAT_IOS: the UIScene lifecycle shell.
    #
    # Against the iOS 26+ SDK, UIKit traps an app that runs the legacy
    # UIApplicationDelegate-only lifecycle
    # (_UIApplicationEvaluateRuntimeIssueForNoSceneLifecycleAdoption, SIGTRAP)
    # before main() runs. SDL 2.32.10's UIKit backend has no UIScene code, so
    # this target cannot launch on a current iOS without a scene delegate.
    #
    # PaperBoatIosShell.m implements that delegate (named by ios/plist.in) and
    # grafts the SDL-created UIWindow onto the connected scene — a window with
    # windowScene == nil never appears, so the graft is what makes the app
    # visible, not just launchable. The sources live outside the upstream tree
    # and are passed in by the port's build scripts.
    set(PAPERBOAT_IOS_SHELL_DIR "" CACHE PATH "Directory holding the iOS app shell sources")
    if(NOT EXISTS "${PAPERBOAT_IOS_SHELL_DIR}/PaperBoatIosShell.m")
        message(FATAL_ERROR "iOS build requires -DPAPERBOAT_IOS_SHELL_DIR=<dir containing PaperBoatIosShell.m>")
    endif()
    target_sources(${PROJECT_NAME} PRIVATE "${PAPERBOAT_IOS_SHELL_DIR}/PaperBoatIosShell.m")
    target_include_directories(${PROJECT_NAME} PRIVATE "${PAPERBOAT_IOS_SHELL_DIR}")
    # ARC for the shell only: the rest of the target is C/C++ and must not be
    # touched by an ARC-wide Xcode attribute.
    set_source_files_properties("${PAPERBOAT_IOS_SHELL_DIR}/PaperBoatIosShell.m"
        PROPERTIES COMPILE_FLAGS "-fobjc-arc")

    set_target_properties(${PROJECT_NAME} PROPERTIES
"""

# --------------------------------------------------------------------- plist.in
PLIST_OLD = """	<key>UILaunchStoryboardName</key>
"""

PLIST_NEW = """	<!-- iOS 26+ requires UIScene lifecycle adoption; without this the app
	     SIGTRAPs inside UIApplicationMain before main() runs. The manifest
	     alone is not enough on its own: the delegate class named here has to
	     exist and take the window (see PaperBoatIosShell.m). -->
	<key>UIApplicationSceneManifest</key>
	<dict>
		<key>UIApplicationSupportsMultipleScenes</key>
		<false/>
		<key>UISceneConfigurations</key>
		<dict>
			<key>UIWindowSceneSessionRoleApplication</key>
			<array>
				<dict>
					<key>UISceneConfigurationName</key>
					<string>Default Configuration</string>
					<key>UISceneClassName</key>
					<string>UIWindowScene</string>
					<key>UISceneDelegateClassName</key>
					<string>PBIosSceneDelegate</string>
				</dict>
			</array>
		</dict>
	</dict>
	<key>UILaunchStoryboardName</key>
"""

body = edit("CMakeLists.txt", CMAKE_OLD, CMAKE_NEW) + edit("ios/plist.in", PLIST_OLD, PLIST_NEW)
OUT.write_text(__doc__ + "\n" + body)
print(f"wrote {OUT}")
