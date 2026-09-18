#!/usr/bin/env python3
"""Overlay patch 0005 — our branding, VERSION plumbing and a compiled-in build stamp.

CLASS: (c) our branding.

Four vendor files, one idea: the PORT owns its identity and its version number,
and both are passed in from outside rather than inherited from upstream's
`project(Paperboat VERSION 1.0.0)`.

1. `CMakeLists.txt`, `if(IOS)` block — the bundle id becomes a cache variable
   defaulting to `com.rebelancap.paperboat` (program spec; upstream's
   `dev.net64.paperboat` is not ours to publish under).

2. `CMakeLists.txt`, just before `configure_file(build.c.in ...)` —
   `PAPERBOAT_IOS_VERSION` and `PAPERBOAT_IOS_BUILD` cache variables, with
   defaults, so a plain upstream `cmake -B build` still configures. The build
   scripts pass `-DPAPERBOAT_IOS_VERSION=$(cat VERSION)` and
   `-DPAPERBOAT_IOS_BUILD=$(date -u +%Y%m%d%H%M)`.

   Why not `CMAKE_PROJECT_VERSION`: SideStore decides "is there an update" by
   string-comparing `CFBundleShortVersionString`. Upstream bumps rarely, so
   publishing upstream's number strands users on whatever they first installed
   (the publishing conventions §1). `CFBundleShortVersionString` is therefore the
   port's public version (`VERSION` file, moves only when a release is cut) and
   `CFBundleVersion` is a UTC build stamp that moves every build — iOS can skip
   a reinstall when both strings are identical, which reads as "I installed the
   new build and got the old one" during OTA testing.

3. `src/port/build.c.in` / `src/port/build.h` — the build stamp itself, compiled
   into the binary as `gPortBuildStamp`. The program's rule is that a build
   stamp is generated source compiled in, asserted post-build and at launch;
   upstream already has this file for `gBuildVersion`, so the port's stamp rides
   beside it rather than inventing a second mechanism. `app/ios/PaperBoatIosShell.m`
   (this repo) logs it, and the `Info.plist` values beside it, at `+load` time.

4. `ios/plist.in` — `CFBundleShortVersionString`/`CFBundleVersion` from those two
   variables instead of `@PROJECT_VERSION@` twice, and `CFBundleDisplayName`
   "PaperBoat" instead of `$(EXECUTABLE_NAME)` ("Paperboat", upstream's target
   name and the wrong capitalisation for the home screen).

None of the four edits touch a code path: the bundle id and the plist are build
metadata, and the stamp is a new symbol nothing upstream reads.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0005-paperboat-ios-branding-version.patch"

EDITS = []

# ---------------------------------------------------------------- CMakeLists.txt
CMAKE_BUNDLE_OLD = """    set(BUNDLE_ID "dev.net64.paperboat")
"""
CMAKE_BUNDLE_NEW = """    # PAPERBOAT_IOS: our bundle id, as a cache variable so upstream's default is
    # one -D away and a plain configure still works. dev.net64.paperboat is
    # upstream's identity; this port publishes under the program's.
    set(PAPERBOAT_IOS_BUNDLE_IDENTIFIER "com.rebelancap.paperboat" CACHE STRING
        "iOS bundle identifier for this port")
    set(BUNDLE_ID "${PAPERBOAT_IOS_BUNDLE_IDENTIFIER}")
"""

CMAKE_VERSION_OLD = """################################################################################
# Sources
################################################################################
configure_file( ${CMAKE_CURRENT_SOURCE_DIR}/src/port/build.c.in ${CMAKE_CURRENT_SOURCE_DIR}/src/port/build.c @ONLY)
"""
CMAKE_VERSION_NEW = """################################################################################
# Port versioning — deliberately NOT ${PROJECT_VERSION}.
#
# PAPERBOAT_IOS_VERSION is the PUBLIC version (CFBundleShortVersionString), the
# string an installer compares to decide "is there an update": it moves only
# when a release is cut, and stays put across test builds. PAPERBOAT_IOS_BUILD
# is the build number (CFBundleVersion) and moves EVERY build, which is what
# distinguishes those iterations and what makes iOS treat each install as new.
# Both come from the port's build scripts (a VERSION file + a UTC timestamp);
# the defaults below exist so a plain upstream configure still succeeds.
################################################################################
set(PAPERBOAT_IOS_VERSION "0.0.0" CACHE STRING "Public (marketing) version of this port")
set(PAPERBOAT_IOS_BUILD "0" CACHE STRING "Build number; must increase every build")

################################################################################
# Sources
################################################################################
configure_file( ${CMAKE_CURRENT_SOURCE_DIR}/src/port/build.c.in ${CMAKE_CURRENT_SOURCE_DIR}/src/port/build.c @ONLY)
"""

EDITS.append(("CMakeLists.txt", [(CMAKE_BUNDLE_OLD, CMAKE_BUNDLE_NEW, 1),
                                 (CMAKE_VERSION_OLD, CMAKE_VERSION_NEW, 1)]))

# ------------------------------------------------------------ src/port/build.c.in
BUILDC_OLD = """const char gBuildTeam[] = "@PROJECT_TEAM@";
"""
BUILDC_NEW = """const char gBuildTeam[] = "@PROJECT_TEAM@";

// PAPERBOAT_IOS: the port's own build stamp, compiled in as generated source so
// a built binary can always be asked which build it is — asserted post-build
// (plutil over the bundle's Info.plist) and logged at launch.
const char gPortBuildStamp[] =
    "PaperBoat-ios @PAPERBOAT_IOS_VERSION@ (build @PAPERBOAT_IOS_BUILD@, upstream @PROJECT_VERSION@)";
"""

EDITS.append(("src/port/build.c.in", [(BUILDC_OLD, BUILDC_NEW, 1)]))

# -------------------------------------------------------------- src/port/build.h
BUILDH_OLD = """extern char gBuildTeam[];
"""
BUILDH_NEW = """extern char gBuildTeam[];
// PAPERBOAT_IOS: "PaperBoat-ios <public version> (build <utc stamp>, upstream <x.y.z>)"
extern char gPortBuildStamp[];
"""

EDITS.append(("src/port/build.h", [(BUILDH_OLD, BUILDH_NEW, 1)]))

# ----------------------------------------------------------------- ios/plist.in
PLIST_NAME_OLD = """\t<key>CFBundleDisplayName</key>
\t<string>$(EXECUTABLE_NAME)</string>
"""
PLIST_NAME_NEW = """\t<key>CFBundleDisplayName</key>
\t<string>PaperBoat</string>
"""

PLIST_VER_OLD = """\t<key>CFBundleShortVersionString</key>
\t<string>@PROJECT_VERSION@</string>
\t<key>CFBundleVersion</key>
\t<string>@PROJECT_VERSION@</string>
"""
PLIST_VER_NEW = """\t<!-- The port's public version and build number, not upstream's project
\t     version: see the PAPERBOAT_IOS_VERSION block in CMakeLists.txt. Two
\t     identical strings here let iOS skip a reinstall. -->
\t<key>CFBundleShortVersionString</key>
\t<string>@PAPERBOAT_IOS_VERSION@</string>
\t<key>CFBundleVersion</key>
\t<string>@PAPERBOAT_IOS_BUILD@</string>
"""

EDITS.append(("ios/plist.in", [(PLIST_NAME_OLD, PLIST_NAME_NEW, 1),
                               (PLIST_VER_OLD, PLIST_VER_NEW, 1)]))

chunks = []
for rel, subs in EDITS:
    src = VENDOR / rel
    orig = src.read_text()
    text = orig
    for old, new, expect in subs:
        n = text.count(old)
        assert n == expect, f"{rel}: expected {expect} match(es) of {old!r:.60}, got {n}"
        text = text.replace(old, new)
    with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
         tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
        fa.write(orig); fb.write(text); fa.flush(); fb.flush()
        r = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}",
                            fa.name, fb.name], capture_output=True)
    assert r.returncode == 1, f"{rel}: diff produced no change"
    chunks.append(r.stdout.decode())

OUT.write_text(__doc__ + "\n" + "".join(chunks))
print(f"wrote {OUT}")
