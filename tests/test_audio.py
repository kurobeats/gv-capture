#!/usr/bin/env python3
"""Audio-focused hardware tests. Run ON the capture host:

    python3 tests/test_audio.py            # full suite
    python3 tests/test_audio.py --quick    # shorter captures

Audio is where this tool keeps breaking, so these tests inject a *known* signal
instead of relying on whatever the card happens to output:

    ffmpeg sine -> module-null-sink -> gv-capture reads <sink>.monitor

That makes gain, channel mapping, routing, monitor and silence handling checkable
to the dB, silently (the null sink is not connected to the speakers). The pulse
module is unloaded afterwards, also on failure.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "gv-capture.py"
QUICK = "--quick" in sys.argv
RUN = 4 if QUICK else 6
TONE_DB = -6  # sine source is already at 1/8 full scale (-18 dBFS), so -24 dBFS total
EXPECTED_PEAK = -24.0
RESULTS = []


class Failed(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise Failed(msg)


def sh(cmd, timeout=60, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def kill_strays():
    for name in ("ffplay", "ffmpeg"):
        subprocess.run(["pkill", "-x", name], capture_output=True)
    time.sleep(1.5)


def strays():
    found = []
    for name in ("ffplay", "ffmpeg"):
        if subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0:
            found.append(name)
    return found


def pulse_env():
    return dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}")


def peak_db(path):
    """Peak level of a file, and its channel count."""
    r = sh(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info", "-i", str(path),
            "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"max_volume: (-?[\d.]+|-inf) dB", r.stderr)
    need(m, f"no volume reading for {path}: {r.stderr[-300:]}")
    lvl = float("-inf") if m.group(1) == "-inf" else float(m.group(1))
    p = sh(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
            "stream=channels", "-of", "csv=p=0", str(path)])
    return lvl, int(p.stdout.strip() or 0)


def channel_peak(path, index):
    """Peak of one channel, extracted with pan (works on any input layout)."""
    r = sh(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info", "-i", str(path),
            "-af", f"pan=mono|c0=c{index},volumedetect", "-f", "null", "-"])
    m = re.search(r"max_volume: (-?[\d.]+|-inf) dB", r.stderr)
    need(m, f"no level for channel {index}: {r.stderr[-300:]}")
    return float("-inf") if m.group(1) == "-inf" else float(m.group(1))


def streams(path, codec_type):
    r = sh(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
            "-show_streams", str(path)])
    need(r.returncode == 0, f"ffprobe failed: {r.stderr[:200]}")
    return [s for s in json.loads(r.stdout)["streams"] if s["codec_type"] == codec_type]


class Sink:
    """One pulse null sink, unloaded when the lab closes."""

    def __init__(self, lab, name):
        self.lab, self.name = lab, name
        r = sh(["pactl", "load-module", "module-null-sink", f"sink_name={name}",
                "sink_properties=device.description=gv-capture-test"])
        need(r.returncode == 0 and r.stdout.strip(), f"cannot load null sink {name}: "
             f"{r.stderr}")
        self.module = r.stdout.strip()

    @property
    def monitor(self):
        return f"{self.name}.monitor"

    def tone(self, seconds=14, left_only=True, db=TONE_DB):
        """Start the tone into this sink; kill the process when done.

        PULSE_SINK, not the URL: ffmpeg's pulse output ignores the URL device.
        """
        pan = "pan=stereo|c0=c0|c1=0*c0" if left_only else "pan=stereo|c0=c0|c1=c1"
        return subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
             "-i", f"sine=frequency=1000:sample_rate=48000:duration={seconds}",
             "-af", f"volume={db}dB,{pan}", "-f", "pulse", self.name],
            env=dict(pulse_env(), PULSE_SINK=self.name),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def index(self):
        r = sh(["pactl", "list", "short", "sinks"])
        for line in r.stdout.splitlines():
            f = line.split()
            if len(f) > 1 and f[1] == self.name:
                return int(f[0])
        raise Failed(f"null sink {self.name} not listed")


class Lab:
    """Two null sinks: `src` is recorded from, `out` is where the monitor plays.

    Separate sinks on purpose: monitoring into the sink you record from is a
    feedback loop (and is refused by the tool).
    """

    def __enter__(self):
        self.src = Sink(self, "gvcat-src")
        self.out = Sink(self, "gvcat-out")
        time.sleep(0.8)
        return self

    def __exit__(self, *exc):
        for sink in (getattr(self, "src", None), getattr(self, "out", None)):
            if sink is not None:
                sh(["pactl", "unload-module", sink.module])
        time.sleep(0.5)
        return False

    def env(self):
        """gv-capture settings that make it record the tone and monitor to `out`."""
        return {"GV_AUDIO": self.src.monitor, "GV_SINK": self.out.name}

def capture(out, extra=(), env=None, run_for=RUN, sig=signal.SIGINT, log_dir=None):
    log = Path(log_dir or tempfile.gettempdir()) / f"{Path(out).name}.log"
    full_env = pulse_env()
    full_env.update(env or {})
    proc = subprocess.Popen([sys.executable, str(SCRIPT), str(out), *extra],
                            stdout=log.open("w"), stderr=subprocess.STDOUT, env=full_env)
    time.sleep(run_for)
    if sig is not None and proc.poll() is None:
        proc.send_signal(sig)
    try:
        rc = proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise Failed("capture did not exit")
    return rc, log.read_text(errors="replace").replace("\r", "\n")


def sink_inputs_after(delay):
    """pactl's sink-inputs lines after `delay` seconds, as (sink id, line) pairs."""
    time.sleep(delay)
    r = sh(["pactl", "list", "short", "sink-inputs"])
    rows = []
    for line in r.stdout.splitlines():
        f = line.split()
        if len(f) >= 2 and f[1].isdigit():
            rows.append((int(f[1]), line))
    return rows


# -------------------------------------------------------------------- tests
def t_monitor_routes_to_gv_sink(tmp):
    """The recurring bug: the monitor played to the default sink whatever we asked for."""
    with Lab() as lab:
        want = lab.out.index()
        out = tmp / "route.mkv"
        env = lab.env()
        log = Path(tempfile.gettempdir()) / "route.log"
        proc = subprocess.Popen([sys.executable, str(SCRIPT), str(out), "PAL", "mono"],
                                stdout=log.open("w"), stderr=subprocess.STDOUT,
                                env=dict(pulse_env(), **env))
        try:
            rows = sink_inputs_after(3.5)
            mine = [line for sink, line in rows if sink == want and "s16le" in line]
            need(mine, f"monitor is not playing on sink {want}: {rows}")
            detail = f"monitor on the requested sink (id {want})"
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=60)
        need(proc.returncode == 0, f"exit {proc.returncode}: {log.read_text()[-400:]}")
    return detail


def t_monitor_death_does_not_stop_recording(tmp):
    """A monitor that dies must not take the recording with it."""
    with Lab() as lab:
        out = tmp / "mondead.mkv"
        log = Path(tempfile.gettempdir()) / "mondead.log"
        proc = subprocess.Popen([sys.executable, str(SCRIPT), str(out), "PAL"],
                                stdout=log.open("w"), stderr=subprocess.STDOUT,
                                env=dict(pulse_env(), **lab.env()))
        try:
            time.sleep(3)
            killers = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if (entry / "comm").read_text().strip() != "ffmpeg":
                        continue
                    cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
                except OSError:
                    continue
                # the monitor reads pulse and writes pulse; the recorder reads v4l2
                if "pulse" in cmdline and "v4l2" not in cmdline:
                    killers.append(int(entry.name))
            need(killers, "no monitor process found to kill")
            for pid in killers:
                os.kill(pid, signal.SIGKILL)
            time.sleep(3)
            need(proc.poll() is None, "recording died with the monitor")
            proc.send_signal(signal.SIGINT)
            rc = proc.wait(timeout=60)
        except Exception:
            proc.kill()
            raise
        text = log.read_text(errors="replace").replace("\r", "\n")
        need(rc == 0, f"exit {rc}: {text[-400:]}")
        need("audio monitor stopped" in text, f"no warning about the dead monitor: {text[-400:]}")
        need(streams(out, "audio"), "mkv lost its audio track")
    return "monitor killed mid-run: recording continued, exit 0, audio intact"


def t_mono_gain_is_unity(tmp):
    """-6 dBFS (i.e. -24 dBFS from lavfi) left-only tone must survive at full level."""
    with Lab() as lab:
        out = tmp / "gain.mkv"
        tone = lab.src.tone(seconds=14)
        try:
            time.sleep(1.5)
            rc, log = capture(out, ["PAL", "mono"],
                              env=lab.env())
        finally:
            tone.kill()
            tone.wait()
        need(rc == 0, f"exit {rc}: {log[-400:]}")
        lvl, chans = peak_db(out)
        need(chans == 1, f"mono mode produced {chans}ch")
        need(abs(lvl - EXPECTED_PEAK) <= 2.0,
             f"peak {lvl:.1f} dBFS, expected {EXPECTED_PEAK:.1f} "
             f"(an average downmix would give {EXPECTED_PEAK - 6:.1f})")
        detail = f"mono peak {lvl:.1f} dBFS (expected {EXPECTED_PEAK:.1f}, no -6 dB loss)"
    return detail


def t_stereo_keeps_both_channels(tmp):
    """stereo mode must not silently collapse to mono, and must keep the dead channel."""
    with Lab() as lab:
        out = tmp / "stereo.mkv"
        tone = lab.src.tone(seconds=14, left_only=True)
        try:
            time.sleep(1.5)
            rc, log = capture(out, ["PAL", "stereo"],
                              env=lab.env())
        finally:
            tone.kill()
            tone.wait()
        need(rc == 0, f"exit {rc}: {log[-400:]}")
        lvl, chans = peak_db(out)
        need(chans == 2, f"stereo mode produced {chans}ch")
        left, right = channel_peak(out, 0), channel_peak(out, 1)
        need(abs(left - EXPECTED_PEAK) <= 2.0, f"left channel {left:.1f} dBFS, "
             f"expected {EXPECTED_PEAK:.1f}")
        need(right <= -60, f"right channel should stay silent, got {right:.1f} dBFS")
        detail = f"2ch kept: left {left:.1f} dBFS, right {right:.1f} dBFS"
    return detail


def t_monitor_is_mono_in_mono_mode(tmp):
    """Mono mode must feed the monitor mono, else only one speaker plays."""
    with Lab() as lab:
        out = tmp / "monmono.mkv"
        log = Path(tempfile.gettempdir()) / "monmono.log"
        proc = subprocess.Popen([sys.executable, str(SCRIPT), str(out), "PAL", "mono"],
                                stdout=log.open("w"), stderr=subprocess.STDOUT,
                                env=dict(pulse_env(), **lab.env()))
        try:
            time.sleep(3.5)
            r = sh(["pactl", "list", "short", "sink-inputs"])
            need(any("1ch" in line for line in r.stdout.splitlines()),
                 f"no 1ch monitor stream: {r.stdout!r}")
            detail = "monitor stream is 1ch (both speakers)"
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=60)
    return detail


def t_silent_track_is_reported(tmp):
    """Recording silence must be visibly reported, not discovered days later."""
    with Lab() as lab:  # nothing is injected: the monitor source is pure silence
        out = tmp / "silent.mkv"
        rc, log = capture(out, ["PAL", "mono"],
                          env=lab.env())
    need(rc == 0, f"exit {rc}: {log[-400:]}")
    need("audio track is silent" in log, f"no silence warning: {log[-400:]}")
    return "silent source: warned (mkv still valid)"


def t_audible_track_is_not_flagged(tmp):
    """...and a real signal must not trigger the warning."""
    with Lab() as lab:
        out = tmp / "audible.mkv"
        tone = lab.src.tone(seconds=14)
        try:
            time.sleep(1.5)
            rc, log = capture(out, ["PAL", "mono"],
                              env=lab.env())
        finally:
            tone.kill()
            tone.wait()
    need(rc == 0, f"exit {rc}: {log[-400:]}")
    need("audio track is silent" not in log, f"false silence warning: {log[-400:]}")
    m = re.search(r"audio peak (-?[\d.]+) dBFS", log)
    need(m, f"no audio level in the report: {log[-300:]}")
    return f"reported audio peak {m.group(1)} dBFS, no silence warning"


def t_feedback_loop_is_refused(tmp):
    """Recording a sink's monitor while playing the monitor into that sink would howl."""
    with Lab() as lab:
        out = tmp / "feedback.mkv"
        rc, log = capture(out, ["PAL"],
                          env={"GV_AUDIO": lab.src.monitor, "GV_SINK": lab.src.name})
    need(rc == 0, f"exit {rc}: {log[-400:]}")
    need("avoid feedback" in log, f"no feedback warning: {log[-400:]}")
    need(streams(out, "audio"), "mkv has no audio track")
    return "monitor into the recorded sink refused: no howl loop, recording fine"


def t_monitor_only_sink_missing(tmp):
    """GV_SINK that does not exist: warn, disable the monitor, still record."""
    out = tmp / "badsink.mkv"
    rc, log = capture(out, ["PAL"], env={"GV_SINK": "alsa_output.does-not-exist"})
    need(rc == 0, f"exit {rc}: {log[-400:]}")
    need("is not an audio sink" in log, f"no warning: {log[-400:]}")
    need("done:" in log, "capture did not finish")
    need(streams(out, "audio"), "mkv has no audio track")
    return "bogus GV_SINK: warned, monitoring off, recording fine"


def t_audio_source_switch_is_followed(tmp):
    """A source that moved (USB renumbering) is followed, not fatal."""
    out = tmp / "moved.mkv"
    rc, log = capture(out, ["PAL"], env={"GV_AUDIO": "alsa_input.does-not-exist"})
    need(rc == 0, f"exit {rc}: {log[-400:]}")
    need("using audio source" in log and "GV-USB2" in log, f"no fallback note: {log[-400:]}")
    return "bogus GV_AUDIO falls back to the GV-USB2 source"


def t_audio_track_shape(tmp):
    """The mkv audio must stay aac/48k with exactly one stream."""
    out = tmp / "shape.mkv"
    rc, log = capture(out, ["PAL", "mono"])
    need(rc == 0, f"exit {rc}: {log[-400:]}")
    tracks = streams(out, "audio")
    need(len(tracks) == 1, f"{len(tracks)} audio streams")
    a = tracks[0]
    need(a["codec_name"] == "aac", a["codec_name"])
    need(int(a["sample_rate"]) == 48000, a["sample_rate"])
    need(a["channels"] == 1, a["channels"])
    return "1 aac stream, 48 kHz, mono"


def t_audio_tail_matches_video(tmp):
    """Audio must not run short/long against the video (drift or dropped chunks)."""
    out = tmp / "tail.mkv"
    run_for = 10 if QUICK else 15
    rc, log = capture(out, ["PAL"], run_for=run_for)
    need(rc == 0, f"exit {rc}: {log[-400:]}")

    def last_pts(sel):
        r = sh(["ffprobe", "-v", "error", "-select_streams", sel, "-show_entries",
                "packet=pts_time", "-of", "csv=p=0", str(out)])
        vals = [float(x) for x in r.stdout.split() if x and re.match(r"^[\d.]+$", x)]
        need(vals, f"no packets for {sel}")
        return max(vals)

    v, a = last_pts("v:0"), last_pts("a:0")
    need(abs(v - a) < 0.75, f"A/V tail offset {abs(v - a):.2f}s")
    return f"A/V tail offset {abs(v - a):.2f}s over {run_for}s"


def t_audio_timestamps_monotonic(tmp):
    """The mkv audio must have a strictly increasing timeline (players care).

    One duplicate dts right at stream start is a known pulse/driver artifact and is
    tolerated; anything more is a timestamp regression.
    """
    out = tmp / "ts.mkv"
    run_for = 8 if QUICK else 12
    rc, log = capture(out, ["PAL", "mono"], run_for=run_for)
    need(rc == 0, f"exit {rc}: {log[-400:]}")
    r = sh(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
            "packet=dts_time", "-of", "csv=p=0", str(out)])
    ts = [float(x) for x in r.stdout.split() if x and re.match(r"^-?[\d.]+$", x)]
    need(len(ts) > 100, f"only {len(ts)} audio packets")
    dups = [(i, ts[i]) for i in range(1, len(ts)) if ts[i] <= ts[i - 1]]
    need(len(dups) <= 1, f"{len(dups)} non-monotonic audio timestamps, first: {dups[:3]}")
    detail = f"{len(ts)} audio packets, {len(dups)} start artifact(s)"
    if dups:
        detail += f" at {dups[0][1]:.3f}s"
    return detail


TESTS = [t_monitor_routes_to_gv_sink, t_monitor_death_does_not_stop_recording,
         t_mono_gain_is_unity, t_stereo_keeps_both_channels, t_monitor_is_mono_in_mono_mode,
         t_silent_track_is_reported, t_audible_track_is_not_flagged,
         t_feedback_loop_is_refused, t_monitor_only_sink_missing,
         t_audio_source_switch_is_followed,
         t_audio_track_shape, t_audio_tail_matches_video, t_audio_timestamps_monotonic]


def main():
    missing = [t for t in ("ffmpeg", "ffprobe", "ffplay", "v4l2-ctl", "pactl")
               if not shutil.which(t)]
    video = os.environ.get("GV_VIDEO", "/dev/video3")
    if missing or not Path(video).exists():
        print(f"SKIP: not the capture host (missing: {missing or 'nothing'}, {video} "
              f"{'present' if Path(video).exists() else 'missing'})")
        return 2
    picked = [a for a in sys.argv[1:] if not a.startswith("-")]
    tests = [t for t in TESTS if not picked or t.__name__ in picked]
    kill_strays()
    failures = 0
    with tempfile.TemporaryDirectory(prefix="gvaudio-") as td:
        try:
            for fn in tests:
                try:
                    print(f"PASS {fn.__name__:34s} {fn(Path(td))}")
                except Failed as e:
                    failures += 1
                    print(f"FAIL {fn.__name__:34s} {e}")
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    print(f"ERROR {fn.__name__:33s} {type(e).__name__}: {e}")
                finally:
                    kill_strays()
        finally:
            time.sleep(1)
            kill_strays()
    print(f"{len(tests) - failures}/{len(tests)} audio tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
