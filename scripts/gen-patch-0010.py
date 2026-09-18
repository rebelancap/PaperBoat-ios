#!/usr/bin/env python3
"""Overlay patch 0010 — build the App Intent ("Launch PaperBoat").

CLASS: (a) program-baseline parity.

The program wants deep links AND App Intents from day one. Patch 0009 registered
the `paperboat://` scheme; this one compiles `app/ios/PaperBoatIntents.swift`
into the iOS target so the app also ships a Shortcuts/Siri action.

App Intents is Swift-only — there is no Objective-C surface for it, and the
pre-iOS-16 alternative (a SiriKit `INIntent` with an `.intentdefinition` and its
own app-extension target) is a great deal of build machinery for a verb that
means "open the app". So the target gains one Swift file and CMake gains
`enable_language(Swift)`.

Verified on this box before writing the patch: a minimal C + Swift target
configures and builds with `cmake/ios.paperboat.toolchain.cmake` under the Xcode
generator (`Check for working Swift compiler: … swiftc - works`), so nothing
about the port's toolchain blocks Swift.

The intent writes `paperboat://launch` into `UserDefaults` and sets
`openAppWhenRun`; the shell consumes `paperboat_pending_link` at `+load` and on
every scene activation, so an intent and a tapped link take exactly the same
path (and inherit the same queue-until-the-engine-is-ready behaviour).

Insertion point: after the `IOS_SIGNING` block that closes the `if(IOS)` branch,
deliberately clear of every other patch's context —
`scripts/apply-overlay.sh` probes "already applied" by reverse-applying each
patch at `--fuzz=0`, so two patches sharing three lines of context make the
series non-idempotent.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "CMakeLists.txt"
OUT = ROOT / "overlay/patches/0010-paperboat-ios-app-intents.patch"

src = VENDOR / REL
orig = src.read_text()

OLD = """            XCODE_ATTRIBUTE_CODE_SIGNING_REQUIRED "NO"
            XCODE_ATTRIBUTE_CODE_SIGN_IDENTITY ""
        )
    endif()
"""

NEW = """            XCODE_ATTRIBUTE_CODE_SIGNING_REQUIRED "NO"
            XCODE_ATTRIBUTE_CODE_SIGN_IDENTITY ""
        )
    endif()

    # PAPERBOAT_IOS (overlay 0010): the App Intent, "Launch PaperBoat".
    #
    # App Intents has no Objective-C surface, so this is the one Swift file in
    # the port. It writes a paperboat:// URL into UserDefaults and opens the
    # app; PaperBoatIosShell.m consumes it on the same path as a tapped link,
    # so there is no second mechanism to keep in step.
    if(EXISTS "${PAPERBOAT_IOS_SHELL_DIR}/PaperBoatIntents.swift")
        enable_language(Swift)
        target_sources(${PROJECT_NAME} PRIVATE "${PAPERBOAT_IOS_SHELL_DIR}/PaperBoatIntents.swift")
        set_target_properties(${PROJECT_NAME} PROPERTIES
            XCODE_ATTRIBUTE_SWIFT_VERSION "5.0"
            # CMake hands the target's COMPILE_OPTIONS to EVERY language under
            # the Xcode generator, so upstream's -Wall/-Wextra/-Wno-... reach
            # swiftc, which rejects them outright:
            #   error: Driver threw unknown argument: '-Wall'
            # The Swift file needs no flags of its own, so blank the attribute.
            XCODE_ATTRIBUTE_OTHER_SWIFT_FLAGS ""
            # A mixed ObjC/C++/Swift app has to carry the Swift runtime: the
            # deployment floor here is iOS 16.3 and the libraries are not
            # guaranteed present in the OS image for an app built this way.
            XCODE_ATTRIBUTE_ALWAYS_EMBED_SWIFT_STANDARD_LIBRARIES "YES")
    endif()
"""

n = orig.count(OLD)
assert n == 1, f"{REL}: expected 1 match of the IOS_SIGNING else-block tail, got {n}"
text = orig.replace(OLD, NEW)

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
