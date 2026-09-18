#!/usr/bin/env python3
"""Overlay patch 0009 — the paperboat:// URL scheme.

CLASS: (a) program-baseline parity.

Upstream's `ios/plist.in` has no `CFBundleURLTypes`, so the app owns no scheme
and nothing outside it can ask it to do anything. The program wants deep links
from day one (spec Phase 1, the q2repro/SoH port pattern): they are how a
Shortcut, a home-screen link, an App Intent or `xcrun simctl openurl` reaches a
running or a dead app, and on this port they are also the only way to turn the
console bridge on from a device with no Mac attached.

The plist half is this patch; the behaviour is in the shell
(`app/ios/PaperBoatIosShell.m`), which queues every incoming URL and drains it
when the engine is ready:

    paperboat://launch            bring the app up (the App Intent's verb)
    paperboat://console           start the bridge on :8771 (dev builds only)
    paperboat://console?cmd=...   …and run one console command

The identifier is the bundle id and the scheme is the port's own name; both are
what every sibling registers, and the scheme is the one the program reserved for
this port.

Two delivery details that cost the siblings a day each, and are handled in the
shell rather than here:

  * Under a scene session UIKit delivers URL opens ONLY through
    `scene:openURLContexts:` — the legacy `application:openURL:` never fires,
    and SDL's app delegate has no URL code of its own anyway.
  * A COLD launch through a link does not use that method at all: the URL
    arrives in `scene:willConnectToSession:options:`'s connectionOptions. An
    app that only implements the first one silently just launches.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "ios/plist.in"
OUT = ROOT / "overlay/patches/0009-paperboat-ios-url-scheme.patch"

src = VENDOR / REL
orig = src.read_text()

# Appended at the END of the dict, not next to UILaunchStoryboardName: overlay
# 0001's scene-manifest hunk ends three context lines above that key, and
# apply-overlay.sh's "already applied" probe reverse-applies every patch at
# --fuzz=0 — so an insertion there makes 0001 un-probeable and the NEXT build
# fails with "0001 neither applied nor appliable".
OLD = """\t<key>UISupportedInterfaceOrientations~ipad</key>
\t<array>
\t\t<string>UIInterfaceOrientationLandscapeLeft</string>
\t\t<string>UIInterfaceOrientationLandscapeRight</string>
\t</array>
</dict>
"""

NEW = """\t<key>UISupportedInterfaceOrientations~ipad</key>
\t<array>
\t\t<string>UIInterfaceOrientationLandscapeLeft</string>
\t\t<string>UIInterfaceOrientationLandscapeRight</string>
\t</array>
\t<!-- PAPERBOAT_IOS: the port's URL scheme. paperboat://launch is the one-tap
\t     entry point (App Intents / Shortcuts / a home-screen link);
\t     paperboat://console[?cmd=...] enables the dev console bridge on :8771 and
\t     is not a recognised link at all in a release build, which compiles the
\t     bridge out. Handling lives in app/ios/PaperBoatIosShell.m. -->
\t<key>CFBundleURLTypes</key>
\t<array>
\t\t<dict>
\t\t\t<key>CFBundleURLName</key>
\t\t\t<string>$(PRODUCT_BUNDLE_IDENTIFIER)</string>
\t\t\t<key>CFBundleTypeRole</key>
\t\t\t<string>Editor</string>
\t\t\t<key>CFBundleURLSchemes</key>
\t\t\t<array>
\t\t\t\t<string>paperboat</string>
\t\t\t</array>
\t\t</dict>
\t</array>
</dict>
"""

n = orig.count(OLD)
assert n == 1, f"{REL}: expected 1 match of the ~ipad orientations block, got {n}"
text = orig.replace(OLD, NEW)

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
