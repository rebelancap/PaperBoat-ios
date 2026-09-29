#!/usr/bin/env python3
"""Overlay patch 0026 - the audio reservoir is a setting, and the underrun warning stops shouting.

CLASS: (a) program-baseline parity.

REV 2 (round 26, upstream 1.0.2): ONLY THE RESERVOIR REMAINS.
Upstream 1.0.2 revived the N64 audio manager thread (`nuAuMgr`, #169): audio is
produced on its own thread, driven by retrace messages, and handed to SDL through
`port_aiSetNextBuffer` (`src/port/os/Audio.cpp`). `HandleAudioThread`,
`StartAudioFrame`/`EndAudioFrame` and the `audio queue underran` warning are gone,
and so is the game thread's per-tick block on the audio thread that made the
reservoir a timing budget for VIDEO as well as audio. Part 2 below (the
rate-limited warning) therefore has nothing left to patch and is dropped; the
underrun COUNT for the perf harness moved to overlay 0012 rev 8, which samples
the queue at `port_aiSetNextBuffer`.

Part 1 stays, unchanged in code: `DesiredBuffered` still sets the queue depth
the revived manager aims for (`port_aiGetLength` reports only the excess over it,
and the silence pre-roll is sized from it), so `gSohIos.AudioBufferMs` - which
Settings > iOS exposes (overlay 0021) - keeps doing what users set it to do. The
90 ms ceiling still holds for a new reason: `osAiGetStatus` reports the FIFO full
at 6000 queued samples (`kBackendCapacityFrames`), and a target above ~3000 would
sit too close to it.

(Rev 1 text follows; its description of the game thread blocking on the audio
thread is history as of upstream 1.0.2.)

WHY
---
The user on 0.0.0.5: "i hear some stuttering every ~5 seconds or so in the audio.
maybe the video stutters a bit too at the same time." His device log carries 86
`audio queue underran` warnings in 248 s - 35 clusters, median gap 4.06 s - and
the simulator reproduces it faithfully (22 clusters in 227 s, median 5.56 s;
the measurement log M-025).

`docs/frame-map.md` explains why the two symptoms arrive together and cannot be
separated: `gfx_frame.c` calls `GameEngine_StartAudioFrame()` at the top of every
game frame and `GameEngine_EndAudioFrame()` before the submit, so the audio
thread is woken exactly once per game frame and the game thread blocks on it.
The reservoir is therefore a straight timing budget: if the main loop's period
ever exceeds what is queued, the queue empties, and the same long frame is seen
as well as heard.

That budget is `DesiredBuffered = 1680` samples at 32 kHz = **52.5 ms**, which
is half of what the siblings run (Lighthouse's 0044 made the same number a CVar
for the same reason) and only 1.6 game frames deep. A single 60 ms frame - which
this port takes routinely on a scene change - empties it.

WHAT THIS PATCH DOES
--------------------
1. **`gSohIos.AudioBufferMs` (default 90 ms).** The reservoir in milliseconds,
   converted to samples at init. 90 ms is ~2.7 game frames and, importantly, is
   the largest value this engine can actually hold: `SDLAudioPlayer::DoPlay`
   refuses to queue anything once `Buffered() >= 6000` samples, and the audio
   thread's own skip-when-above guard is `Buffered() > 2 * DesiredBuffered`. Go
   past 3000 samples (93.75 ms) and the guard sits above the hard ceiling, so it
   never fires, the queue parks at 6000 (187 ms of latency) and nothing trims it.
   Clamped to [30, 90] for that reason, with the ceiling named in the comment so
   the next person does not "improve" it to 200.
   Read ONCE, at `InitAudio` - changing it takes a relaunch, which the tooltip
   in Settings > iOS says.

2. **The warning is rate-limited to one per second, and counted instead.**
   Release builds run an `spdlog::async_logger` with `async_overflow_policy::block`
   and `flush_on(info)` (`ship/Context.cpp`), writing to a rotating file sink in
   `Documents/logs/`. Every underrun therefore costs the AUDIO thread a queue
   push and the worker a file flush, while the game thread is blocked on the
   audio cv waiting for that same thread - a small self-amplifying loop at
   exactly the moment the port can least afford one, and 86 identical lines that
   say nothing the count does not. The count goes to the perf harness
   (`underruns` in the `fps` line, overlay 0012 rev2), which is the number to
   watch in an A/B; the log keeps one line per second so a device run with no
   console attached still leaves the evidence.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not touch the produce/skip/refill loop. `docs/lus-divergence.md` already
audited that loop against SYNC WAVE 2 §B and found it correct - the
skip-when-above guard, the bounded refill and the silence pre-roll are all
present and are upstream's own. The reservoir depth was the only number wrong.

UPSTREAMABLE: the rate limit, probably (as a plain `spdlog` rate limit, no
counter). The CVar is ours - upstream's desktop path does not have the problem.
"""
import subprocess, pathlib, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor/PaperBoat"
OUT = ROOT / "overlay/patches/0026-paperboat-ios-audio-reservoir.patch"

REL = "src/port/Engine.cpp"


def replace_once(t, old, new, tag):
    n = t.count(old)
    assert n == 1, f"{REL}: expected 1 match of {tag}, got {n}"
    return t.replace(old, new, 1)


src = VENDOR / REL
orig = src.read_text()
text = orig

# ---------------------------------------------------------------------------
# 1. The reservoir.
# ---------------------------------------------------------------------------
OLD_INIT = """    Ship::Context::GetRawInstance()->InitAudio({ .SampleRate = 32000, .SampleLength = 1024, .DesiredBuffered = 1680 });
"""
NEW_INIT = """#ifdef __IOS__
    // PAPERBOAT_IOS (overlay 0026): the audio reservoir, in milliseconds.
    //
    // Upstream's 1680 samples at 32 kHz is 52.5 ms - 1.6 game frames at 30 Hz.
    // The revived nuAuMgr thread aims the SDL queue at this depth
    // (port_aiGetLength reports only the excess over it), so any stall of that
    // thread longer than the reservoir is an audible click. 90 ms is ~2.7
    // frames of cover.
    //
    // 90 is also the CEILING this engine can use, and the clamp is not
    // cosmetic: SDLAudioPlayer::DoPlay drops anything queued past 6000 samples
    // and osAiGetStatus reports the FIFO full there (os/Audio.cpp); a target
    // above ~3000 samples (93.75 ms) leaves the manager no headroom under that
    // hard ceiling.
    //
    // Read once, here: changing it needs a relaunch (Settings > iOS says so).
    int32_t pbAudioMs = CVarGetInteger("gSohIos.AudioBufferMs", 90);
    if (pbAudioMs < 30) {
        pbAudioMs = 30;
    }
    if (pbAudioMs > 90) {
        pbAudioMs = 90;
    }
    const int32_t pbDesiredBuffered = 32000 * pbAudioMs / 1000;
    SPDLOG_INFO("PB_AUDIO reservoir {} ms = {} samples at 32 kHz", pbAudioMs, pbDesiredBuffered);
    Ship::Context::GetRawInstance()->InitAudio(
        { .SampleRate = 32000, .SampleLength = 1024, .DesiredBuffered = pbDesiredBuffered });
#else
    Ship::Context::GetRawInstance()->InitAudio({ .SampleRate = 32000, .SampleLength = 1024, .DesiredBuffered = 1680 });
#endif
"""
text = replace_once(text, OLD_INIT, NEW_INIT, "InitAudio")

with tempfile.NamedTemporaryFile("w", suffix=".a", delete=False) as fa, \
     tempfile.NamedTemporaryFile("w", suffix=".b", delete=False) as fb:
    fa.write(orig); fb.write(text); fa.flush(); fb.flush()
    r = subprocess.run(["diff", "-u", "--label", f"a/{REL}", "--label", f"b/{REL}",
                        fa.name, fb.name], capture_output=True)
assert r.returncode == 1, "diff produced no change"

OUT.write_text(__doc__ + "\n" + r.stdout.decode())
print(f"wrote {OUT}")
