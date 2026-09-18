#!/usr/bin/env python3
"""Overlay patch 0004 — stop double-prefixing the config path with the app directory.

CLASS: (b) upstream bug fix worth sending.

`Ship::Context` stores the string it is constructed with in `mConfigFilePath`
and uses it in exactly one place (`libultraship/src/ship/Context.cpp:198`):

    mConfig = std::make_shared<Config>(GetPathRelativeToAppDirectory(mConfigFilePath));

i.e. the parameter is a name RELATIVE to the app directory — Context resolves it
itself. `Engine.cpp` hands it an ALREADY-ABSOLUTE
`GetPathRelativeToAppDirectory("paperboat.cfg.json")`, so the app directory is
prefixed twice:

    <appdir>/<appdir>/paperboat.cfg.json

`GetPathRelativeToAppDirectory()` is `GetAppDirectoryPath() + "/" + path`, so on
iOS (where `GetAppDirectoryPath()` is `$HOME/Documents`) the result is

    /.../Documents//Users/.../Documents/paperboat.cfg.json

which does not exist and cannot be created. `Config::Save()` therefore fails on
EVERY write (`Config.cpp:222`, "Could not open ... to save config"), hundreds of
times per run — every CVar change, every menu toggle, the touch-layout editor's
positions, the graphics backend selection. Nothing the user sets survives a
relaunch. The initial LOAD fails just as silently, so the game always starts
from defaults.

It bites on macOS too whenever `SHIP_HOME` is set (the oracle's D4 trap 3,
worked around there with a relative `SHIP_HOME` — a run-script hack, not a fix).
It is invisible in the common desktop case only because `GetAppDirectoryPath()`
then returns SDL's pref path and the doubled path happens to be created by
`Config`'s own `create_directories` on some platforms.

The fix is to pass the relative name, which is what Context documents by use.
Nothing else in the port passes a path INTO libultraship this way: the other
`GetPathRelativeToAppDirectory()` call sites (`mods`, `paperboat-hd.o2r`,
`pm64.o2r`, `default.sav`, `saves/`, `baserom.us.z64`) all consume the absolute
result directly with `std::filesystem`, which is correct, and
`gamecontrollerdb.txt` is resolved inside LUS by `LocateFileAcrossAppDirs`.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
REL = "src/port/Engine.cpp"
OUT = ROOT / "overlay/patches/0004-paperboat-config-path-double-prefix.patch"

src = VENDOR / REL
orig = src.read_text()

OLD = """    this->context = Ship::Context::CreateUninitializedInstance(
        "Paperboat", "boat", Ship::Context::GetPathRelativeToAppDirectory("paperboat.cfg.json")
    );
"""
NEW = """    // PAPERBOAT_IOS: pass the config file name RELATIVE to the app directory.
    // Ship::Context stores this string and later resolves it itself with
    // GetPathRelativeToAppDirectory() (Context.cpp, InitConfiguration), so
    // handing it an absolute path prefixes the app directory twice:
    //   <appdir>/<appdir>/paperboat.cfg.json
    // On iOS that is "$HOME/Documents//Users/.../Documents/paperboat.cfg.json",
    // which cannot be opened, so every settings save fails (Config.cpp "Could
    // not open ... to save config") and no setting ever persists. Same bug on
    // macOS/Linux whenever SHIP_HOME is set.
    this->context = Ship::Context::CreateUninitializedInstance(
        "Paperboat", "boat", "paperboat.cfg.json"
    );
"""

n = orig.count(OLD)
assert n == 1, f"{REL}: expected 1 match of the CreateUninitializedInstance call, got {n}"
text = orig.replace(OLD, NEW)

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
