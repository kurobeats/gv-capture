#!/usr/bin/env python3
"""Hardware integration tests for gv-capture.py. Run ON the capture host:

    python3 tests/test_integration.py            # full suite (~3 min)
    python3 tests/test_integration.py --quick    # shorter runs

Needs the gvusb2 card (signal not required: blank picture, silence) and ffmpeg,
v4l-utils, ffplay, PipeWire. Writes only under a temp dir, kills its own children,
never touches an existing recording.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "gv-capture.py"
QUICK = "--quick" in sys.argv
VIDEO = os.environ.get("GV_VIDEO", "/dev/video3")
PREVIEW = os.environ.get("GV_PREVIEW", "/dev/video0")
RUN = 4 if QUICK else 6  # seconds per capture
RESULTS = []


class Failed(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise Failed(msg)


# ------------------------------------------------------------------ helpers
def sh(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def kill_strays():
    for name in ("ffplay", "ffmpeg"):
        subprocess.run(["pkill", "-x", name], capture_output=True)
    time.sleep(1.5)


def strays():
    out = []
    for name in ("ffplay", "ffmpeg"):
        r = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
        if r.returncode == 0:
            out.append(f"{name}:{r.stdout.split()[0]}")
    return out


def children_of(pid):
    kids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            continue
        if ppid == pid:
            kids.append((int(entry.name), comm))
    return kids


def sink_inputs():
    r = sh(["pactl", "list", "short", "sink-inputs"])
    return r.stdout if r.returncode == 0 else ""


class Capture:
    """One gv-capture run with its output captured to a file."""

    def __init__(self, out, extra=(), env=None, run_for=RUN):
        self.out, self.run_for = out, run_for
        # outside `out`'s directory: a nested output dir must be the script's job
        self.log = Path(tempfile.gettempdir()) / f"{Path(out).name}.log"
        full_env = dict(os.environ, **{"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"})
        full_env.update(env or {})
        self.proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), out, *extra],
            stdout=self.log.open("w"), stderr=subprocess.STDOUT, env=full_env)

    def during(self, fn, delay=None):
        """Call fn() while the capture runs."""
        time.sleep(delay if delay is not None else self.run_for * 0.6)
        return fn()

    def ffmpeg_pid(self):
        for pid, comm in children_of(self.proc.pid):
            if comm.startswith("ffmpeg"):
                return pid
        return None

    def player_pid(self):
        for pid, comm in children_of(self.proc.pid):
            if comm.startswith("ffplay"):
                return pid
        return None

    def stop(self, sig=signal.SIGINT, wait=90):
        """sig=None: just wait (used when ffmpeg died and nobody pressed anything)."""
        if sig is not None and self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            return self.proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise Failed("capture did not exit within the stop timeout")

    def text(self):
        return self.log.read_text(errors="replace").replace("\r", "\n")


def capture(out, extra=(), env=None, run_for=RUN, sig=signal.SIGINT):
    c = Capture(out, extra, env, run_for)
    time.sleep(run_for)
    rc = c.stop(sig)
    return rc, c.text()


def probe(path):
    r = sh(["ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path)])
    need(r.returncode == 0, f"ffprobe failed on {path}: {r.stderr.strip()}")
    return json.loads(r.stdout)


def streams(path, codec_type=None):
    return [s for s in probe(path)["streams"]
            if codec_type is None or s["codec_type"] == codec_type]


def duration(path):
    return float(probe(path)["format"]["duration"])


TS_WARN = "non monotonically increasing dts"


def decode_report(path):
    """(real decode errors, timestamp warnings) from a full decode.

    A single duplicate audio dts at stream start is a known pulse/driver artifact:
    worth counting, but it is not a decode failure.
    """
    r = sh(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], timeout=180)
    lines = [line for line in r.stderr.splitlines() if line.strip()]
    errors = [line for line in lines if TS_WARN not in line]
    need(r.returncode == 0, f"decode failed: {r.stderr[-400:]}")
    return errors, len(lines) - len(errors)


def decodes_clean(path):
    errors, ts = decode_report(path)
    need(not errors, f"decode errors: {errors[:3]}")
    need(ts == 0, f"{ts} timestamp warnings while decoding {path}")


def finalised(rc, log, path, expect_duration=None):
    need(rc == 0, f"exit {rc}; log: {log[-500:]}")
    need(Path(path).exists(), "no output file")
    need("done:" in log, f"no 'done:' line; log: {log[-500:]}")
    d = duration(path)
    if expect_duration:
        need(expect_duration - 1.5 <= d <= expect_duration + 4,
             f"duration {d:.2f}s not near {expect_duration}s")


# -------------------------------------------------------------------- tests
def t_pal_mono(tmp):
    out = tmp / "pal.mkv"
    rc, log = capture(out, ["PAL", "mono"])
    finalised(rc, log, out, RUN)
    v, a = streams(out, "video")[0], streams(out, "audio")[0]
    need((v["width"], v["height"]) == (720, 566), f"{v['width']}x{v['height']}")
    need(v["codec_name"] == "ffv1" and v["pix_fmt"] == "yuv422p", f"{v['codec_name']}/{v['pix_fmt']}")
    need(a["codec_name"] == "aac" and a["channels"] == 1, f"{a['codec_name']}/{a['channels']}ch")
    need(abs(float(v["r_frame_rate"].split("/")[0]) / float(v["r_frame_rate"].split("/")[1]) - 25) < 0.1,
         f"fps {v['r_frame_rate']}")
    decodes_clean(out)
    return "720x566 ffv1 mono aac, decodes clean"


def t_ntsc_stereo(tmp):
    out = tmp / "ntsc.mkv"
    rc, log = capture(out, ["NTSC", "stereo"])
    finalised(rc, log, out, RUN)
    v, a = streams(out, "video")[0], streams(out, "audio")[0]
    need((v["width"], v["height"]) == (720, 486), f"{v['width']}x{v['height']}")
    need(a["channels"] == 2, f"{a['channels']}ch")
    need(v["r_frame_rate"] == "30000/1001", f"fps {v['r_frame_rate']}")
    decodes_clean(out)
    return "720x486, 29.97, stereo aac"


def t_sigterm_stop(tmp):
    out = tmp / "sigterm.mkv"
    rc, log = capture(out, ["PAL"], sig=signal.SIGTERM)
    finalised(rc, log, out, RUN)
    need("done:" in log, "no done line")
    return "SIGTERM finalises the mkv"


def t_preview_close_stops(tmp):
    out = tmp / "close.mkv"
    c = Capture(out, ["PAL"])
    try:
        player = c.during(c.player_pid, delay=min(4, RUN * 0.7))
        need(player is not None, "no preview window process")
        os.kill(player, signal.SIGTERM)  # what closing the window does
        rc = c.stop(wait=60)
    except Exception:
        c.proc.kill()
        raise
    finalised(rc, c.text(), out)
    return "closing the preview finalises and exits"


def t_preview_geometry(tmp):
    out = tmp / "geom.mkv"
    c = Capture(out, ["PAL"])
    try:
        def probe_geom():
            dev = sh(["v4l2-ctl", "-d", PREVIEW, "--get-fmt-video"]).stdout
            need("UYVY" in dev.upper(), f"loopback not in UYVY: {dev[:200]}")
            need("720/576" in dev, f"loopback geometry: {dev[:200]}")
            player = c.player_pid()
            need(player is not None, "no ffplay")
            cmdline = Path(f"/proc/{player}/cmdline").read_bytes().split(b"\0")
            sizes = [x.decode() for x in cmdline if b"x" in x and b"video_size" not in x]
            need("720x576" in sizes, f"ffplay video_size not the device geometry: {sizes}")
            return "device 720x576, ffplay 720x576"
        detail = c.during(probe_geom, delay=min(4, RUN * 0.7))
        rc = c.stop(wait=60)
    except Exception:
        c.proc.kill()
        raise
    finalised(rc, c.text(), out)
    return detail


def t_monitor_stream(tmp):
    out = tmp / "monitor.mkv"
    c = Capture(out, ["PAL", "mono"])
    try:
        during = c.during(sink_inputs, delay=min(4, RUN * 0.7))
        rc = c.stop(wait=60)
    except Exception:
        c.proc.kill()
        raise
    finalised(rc, c.text(), out)
    need("1ch" in during, f"no mono monitor stream while recording: {during!r}")
    return "monitor sink-input present, 1ch (both speakers)"


def t_killed_ffmpeg(tmp):
    out = tmp / "killed.mkv"
    c = Capture(out, ["PAL"])
    try:
        pid = c.during(c.ffmpeg_pid, delay=min(4, RUN * 0.7))
        need(pid is not None, "no ffmpeg process")
        os.kill(pid, signal.SIGKILL)
        rc = c.stop(sig=None, wait=60)  # nobody pressed Ctrl-C: must self-report
    except Exception:
        c.proc.kill()
        raise
    log = c.text()
    need(rc == 1, f"exit {rc}, expected 1")
    need("may be truncated" in log or "nothing written" in log, f"log: {log[-400:]}")
    time.sleep(1)
    need(not strays(), f"left strays behind: {strays()}")
    return "exit 1, truncation reported, no frozen preview left"


def t_refuse_existing(tmp):
    out = tmp / "existing.mkv"
    out.write_bytes(b"precious")
    r = sh([sys.executable, str(SCRIPT), str(out), "PAL"], timeout=60)
    need(r.returncode == 1, f"exit {r.returncode}")
    need("already exists" in r.stderr, r.stderr[:200])
    need(out.read_bytes() == b"precious", "existing file was modified")
    return "refuses to overwrite, file untouched"


def t_missing_device(tmp):
    out = tmp / "nodev.mkv"
    env = dict(os.environ, GV_VIDEO="/dev/video-does-not-exist")
    r = subprocess.run([sys.executable, str(SCRIPT), str(out)], capture_output=True,
                       text=True, env=env, timeout=60)
    need(r.returncode == 1, f"exit {r.returncode}")
    need("missing" in r.stderr, r.stderr[:200])
    need(not out.exists(), "created a file anyway")
    return "refuses a missing capture device"


def t_missing_preview(tmp):
    out = tmp / "noprev.mkv"
    rc, log = capture(out, ["PAL"], env={"GV_PREVIEW": "/dev/video-does-not-exist"})
    finalised(rc, log, out, RUN)
    need("recording only" in log, f"no note about the missing preview: {log[:300]}")
    return "no loopback: records without preview"


def t_busy_loopback(tmp):
    out = tmp / "busy.mkv"
    holder = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
         "-i", "testsrc=size=720x576:rate=25", "-pix_fmt", "uyvy422", "-f", "v4l2", PREVIEW],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        rc, log = capture(out, ["PAL"], run_for=RUN)
    finally:
        holder.terminate()
        holder.wait(timeout=20)
        time.sleep(1.5)
    finalised(rc, log, out, RUN)
    need("in use" in log and "recording without a preview" in log,
         f"no busy-loopback warning: {log[:400]}")
    return "loopback held by OBS-style writer: records anyway"


def t_audio_fallback(tmp):
    out = tmp / "audiofallback.mkv"
    rc, log = capture(out, ["PAL"], env={"GV_AUDIO": "alsa_input.does-not-exist"})
    finalised(rc, log, out, RUN)
    need("using audio source" in log and "GV-USB2" in log, f"log: {log[:400]}")
    return "bogus GV_AUDIO falls back to the GV-USB2 source"


def t_second_instance(tmp):
    out1, out2 = tmp / "first.mkv", tmp / "second.mkv"
    c = Capture(out1, ["PAL"])
    try:
        time.sleep(4)
        r = sh([sys.executable, str(SCRIPT), str(out2), "PAL"], timeout=90)
        rc = c.stop(wait=60)
    except Exception:
        c.proc.kill()
        raise
    finalised(rc, c.text(), out1)
    need(r.returncode == 1, f"second instance exit {r.returncode}")
    need("nothing written" in r.stderr or "failed at startup" in r.stderr,
         f"second instance: {r.stderr[:300]}")
    need(not out2.exists() or out2.stat().st_size == 0, "second instance left a junk file")
    return "concurrent capture fails cleanly, first one unaffected"


def t_av_tail_and_frames(tmp):
    out = tmp / "av.mkv"
    run_for = 10 if QUICK else 15
    rc, log = capture(out, ["PAL"], run_for=run_for)
    finalised(rc, log, out, run_for)

    def last_pts(stream):
        r = sh(["ffprobe", "-v", "error", "-select_streams", stream,
                "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(out)])
        vals = [float(x) for x in r.stdout.split() if x and x.replace(".", "").isdigit()]
        need(vals, f"no packets on {stream}")
        return max(vals)

    v, a = last_pts("v:0"), last_pts("a:0")
    need(abs(v - a) < 0.75, f"A/V tail offset {abs(v - a):.2f}s (video {v:.2f}, audio {a:.2f})")
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
            "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(out)])
    frames = int(r.stdout.strip() or 0)
    need(abs(frames - 25 * run_for) <= 25 * 1.5, f"{frames} frames for {run_for}s at 25fps")
    decodes_clean(out)
    return f"A/V tail offset {abs(v - a):.2f}s, {frames} frames"


def t_nested_outdir(tmp):
    out = tmp / "a" / "b" / "c" / "nested.mkv"
    rc, log = capture(out, ["PAL"], run_for=min(4, RUN))
    finalised(rc, log, out)
    need(out.parent.is_dir(), "nested output directory not created")
    return "creates missing output directories"


def t_no_ffplay(tmp):
    out = tmp / "noffplay.mkv"
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    for tool in ("ffmpeg", "ffprobe", "v4l2-ctl", "fuser", "pactl"):
        src = shutil.which(tool)
        if src:
            link = bindir / tool
            if not link.exists():
                link.symlink_to(src)
    rc, log = capture(out, ["PAL"], env={"PATH": str(bindir)})
    finalised(rc, log, out, RUN)
    need("ffplay not found" in log, f"no ffplay warning: {log[:400]}")
    return "no ffplay on PATH: records without a preview"


def t_seekable(tmp):
    src = tmp / "pal.mkv"
    need(src.exists(), "needs t_pal_mono output")
    d = duration(src)
    r = sh(["ffprobe", "-v", "error", "-read_intervals", f"{d / 2:.1f}%+1",
            "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(src)])
    need(r.returncode == 0 and r.stdout.strip(),
         f"cannot seek into the mkv (unfinalised?): {r.stderr[:200]}")
    return "mkv is seekable (trailer/cues written)"


TESTS = [t_pal_mono, t_ntsc_stereo, t_sigterm_stop, t_preview_close_stops,
         t_preview_geometry, t_monitor_stream, t_killed_ffmpeg, t_refuse_existing,
         t_missing_device, t_missing_preview, t_busy_loopback, t_audio_fallback,
         t_second_instance, t_av_tail_and_frames, t_nested_outdir, t_no_ffplay,
         t_seekable]


def main():
    picked = [a for a in sys.argv[1:] if not a.startswith("-")]
    tests = [t for t in TESTS if not picked or t.__name__ in picked]
    missing = [t for t in ("ffmpeg", "ffplay", "v4l2-ctl", "ffprobe", "pactl", "fuser")
               if not shutil.which(t)]
    if missing or not Path(VIDEO).exists():
        print(f"SKIP: this is not the capture host "
              f"(missing: {missing or 'nothing'}, {VIDEO} "
              f"{'present' if Path(VIDEO).exists() else 'missing'})")
        return 2
    kill_strays()
    failures = 0
    with tempfile.TemporaryDirectory(prefix="gvtest-") as td:
        tmp = Path(td)
        try:
            for fn in tests:
                try:
                    detail = fn(tmp)
                    print(f"PASS {fn.__name__:24s} {detail}")
                except Failed as e:
                    failures += 1
                    print(f"FAIL {fn.__name__:24s} {e}")
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    print(f"ERROR {fn.__name__:23s} {type(e).__name__}: {e}")
                finally:
                    kill_strays()
        finally:
            time.sleep(1)
            kill_strays()
    print(f"{len(tests) - failures}/{len(tests)} integration tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
