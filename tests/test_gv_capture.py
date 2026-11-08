#!/usr/bin/env python3
"""Offline self-check for gv-capture.py: no hardware, no ffmpeg needed.

    python3 tests/test_gv_capture.py
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gv_capture", ROOT / "gv-capture.py")
gv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gv)

CHECKS = []


def check(fn):
    """Register a test; a raised AssertionError names it in the failure report."""
    CHECKS.append(fn)
    return fn


@contextlib.contextmanager
def patch(**attrs):
    """Temporarily replace module globals (and gv.subprocess.*) for one test."""
    owner = attrs.pop("owner", gv)
    old = {k: getattr(owner, k) for k in attrs}
    for k, v in attrs.items():
        setattr(owner, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(owner, k, v)


def expect_exit(code, fn, *a):
    """Run fn(*a); assert it exits with `code` and return what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stderr(out), contextlib.redirect_stdout(out):
        try:
            fn(*a)
        except SystemExit as e:
            assert e.code == code, (fn.__name__, e.code, out.getvalue())
            return out.getvalue()
        raise AssertionError(f"{fn.__name__}{a} did not exit {code}")


def fake_run(rc=0, stdout="", stderr=""):
    def run(cmd, **kw):
        run.cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    run.cmds = []
    return run


# ---------------------------------------------------------------- arguments
@check
def args_defaults_and_positionals():
    assert gv.parse_args([]) == (None, "PAL", False)
    assert gv.parse_args(["/tmp/a.mkv"]) == ("/tmp/a.mkv", "PAL", False)
    assert gv.parse_args(["/tmp/a.mkv", "ntsc", "STEREO"]) == ("/tmp/a.mkv", "NTSC", True)
    assert gv.parse_args(["stereo", "/tmp/b.mkv", "PAL"]) == ("/tmp/b.mkv", "PAL", True)
    assert gv.parse_args(["/tmp/c.mkv", "mono"]) == ("/tmp/c.mkv", "PAL", False)


@check
def args_refuse_ambiguity_and_junk():
    for bad in (["/tmp/a.mkv", "/tmp/b.mkv"], ["PAL", "NTSC"], ["--nope"], ["-x"]):
        expect_exit(2, gv.parse_args, bad)


@check
def args_help_prints_usage():
    assert "usage: gv-capture.py" in expect_exit(0, gv.parse_args, ["-h"])
    assert "usage: gv-capture.py" in expect_exit(0, gv.parse_args, ["--help"])


# ---------------------------------------------------------------- geometry
@check
def geometry_from_v4l2_dumps():
    capture = "Format Video Capture:\n\tWidth/Height      : 720/576\n\tPixel Format      : 'UYVY'\n"
    output = "Format Video Output:\n\tWidth/Height      : 720/486\n"
    assert gv.parse_geometry(capture) == (720, 576)
    assert gv.parse_geometry(output) == (720, 486)
    for junk in ("", "nonsense", "Width/Height : x/y"):
        assert gv.parse_geometry(junk) is None


@check
def v4l2_fmt_returns_text_only_on_success():
    with patch(owner=gv.subprocess, run=fake_run(rc=255)):
        assert gv.v4l2_fmt("/dev/video0") == ""
    with patch(owner=gv.subprocess, run=fake_run(rc=0, stdout="Width/Height : 720/576")):
        assert "720" in gv.v4l2_fmt("/dev/video3")


# ---------------------------------------------------------------- profiles
@check
def profiles_are_consistent():
    assert set(gv.PROFILES) == {"PAL", "NTSC"}
    for p in gv.PROFILES.values():
        w, h = (int(v) for v in p["size"].split("x"))
        rw, rh = (int(v) for v in p["recsize"].split("x"))
        assert float(p["fps"]) > 0 and p["vf"], p
        assert rw == w and 0 < rh <= h, p  # a crop may only trim rows
    assert gv.PROFILES["PAL"]["recsize"] == "720x566"


# ---------------------------------------------------------------- ffmpeg cmd
@check
def cmd_full_shape():
    cmd = gv.ffmpeg_cmd("/tmp/out.mkv", "PAL", False, gv.PROFILES["PAL"], "src", True)
    assert cmd[0] == "ffmpeg" and "-nostdin" in cmd and "-n" in cmd  # never clobber
    assert cmd[cmd.index("-vf") + 1] == "setfield=tff,crop=720:566:0:8"
    assert cmd[cmd.index("-i") + 1] == gv.VIDEO
    assert cmd.count("-map") == 3, cmd  # video+audio to the file, video to the preview
    assert cmd.index("/tmp/out.mkv") < cmd.index(gv.PREVIEW)
    assert "-y" not in cmd


@check
def cmd_mono_maps_left_channel_on_file():
    cmd = gv.ffmpeg_cmd("/tmp/out.mkv", "PAL", False, gv.PROFILES["PAL"], "src", True)
    assert cmd[cmd.index("-af") + 1] == gv.MONO_MAP
    assert cmd.index("-af") < cmd.index("/tmp/out.mkv")


@check
def cmd_stereo_leaves_channels_alone():
    cmd = gv.ffmpeg_cmd("/tmp/o.mkv", "NTSC", True, gv.PROFILES["NTSC"], "src", False)
    assert "-af" not in cmd and "-ac" not in cmd


@check
def cmd_optional_outputs():
    no_prev = gv.ffmpeg_cmd("/tmp/o.mkv", "NTSC", True, gv.PROFILES["NTSC"], "src", False)
    assert no_prev.count("-map") == 2 and gv.PREVIEW not in no_prev
    assert "pulse" in no_prev  # the audio input stays; the monitor is a separate process


# ---------------------------------------------------------------- preflight
@check
def preflight_refuses_bad_starts():
    with tempfile.TemporaryDirectory() as td:
        dev = Path(td, "video3")
        dev.write_bytes(b"")
        good = str(Path(td, "out.mkv"))
        with patch(owner=gv.shutil, which=lambda _t: None):
            assert "not found" in expect_exit(1, gv.preflight, good)
        with patch(VIDEO="/dev/video-does-not-exist", owner=gv), \
             patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t):
            assert "missing" in expect_exit(1, gv.preflight, good)
        Path(td, "out.mkv").write_bytes(b"x")
        with patch(VIDEO=str(dev), owner=gv), \
             patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t):
            assert "already exists" in expect_exit(1, gv.preflight, good)


@check
def check_space_thresholds():
    Du = namedtuple("Du", "total used free")
    gib = 2 ** 30
    with tempfile.TemporaryDirectory() as td:
        with patch(owner=gv.shutil, disk_usage=lambda _p: Du(0, 0, 0.5 * gib)):
            assert "only 0.5 GiB free" in expect_exit(1, gv.check_space, str(Path(td, "a.mkv")))
        with patch(owner=gv.shutil, disk_usage=lambda _p: Du(0, 0, 5 * gib)):
            out = io.StringIO()
            with contextlib.redirect_stderr(out):
                gv.check_space(str(Path(td, "a.mkv")))
            assert "warning: only 5.0 GiB" in out.getvalue(), out.getvalue()
        with patch(owner=gv.shutil, disk_usage=lambda _p: Du(0, 0, 500 * gib)):
            out = io.StringIO()
            with contextlib.redirect_stderr(out):
                gv.check_space(str(Path(td, "a.mkv")))
            assert out.getvalue() == ""


# ---------------------------------------------------------------- pulse
@check
def audio_source_prefers_config_and_falls_back():
    gv_audio = gv.AUDIO
    with patch(owner=gv.shutil, which=lambda _t: None):
        assert gv.audio_source() == gv_audio  # no pactl: trust the config
    def listing(*names):
        return "".join(f"{i}\t{n}\talsa\tx\tRUNNING\n" for i, n in enumerate(names))

    with patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=fake_run(rc=0, stdout=listing(
             gv_audio, "alsa_input.usb-I-O_DATA_GV-USB2_9-02.stereo-fallback"))):
        assert gv.audio_source() == gv_audio  # configured source still present
    moved = "alsa_input.usb-I-O_DATA_GV-USB2_9-02.stereo-fallback"
    with patch(AUDIO="alsa_input.gone", owner=gv), \
         patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=fake_run(rc=0, stdout=listing(moved))):
        assert gv.audio_source() == moved  # card renumbered: follow it
    with patch(AUDIO="alsa_input.gone", owner=gv), \
         patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=fake_run(rc=0, stdout=listing("sink.monitor"))):
        assert "no GV-USB2 audio source" in expect_exit(1, gv.audio_source)
    with patch(AUDIO="alsa_input.gone", owner=gv), \
         patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=fake_run(rc=3)):
        assert gv.audio_source() == "alsa_input.gone"  # pactl broken: trust config


@check
def default_sink_resolution():
    sinks = "1\talsa_output.pci.analog-stereo\tx\n2\talsa_output.usb-headset\tx\n"
    with patch(owner=gv.shutil, which=lambda _t: None):
        assert gv.default_sink() == ("default", None)  # no pactl: let pulse decide
    with patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=fake_run(rc=3)):
        assert gv.default_sink() == ("default", None)
    with patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=fake_run(rc=0, stdout="")):
        sink, note = gv.default_sink()
        assert sink is None and "no audio sink" in note
    with patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=fake_run(rc=0, stdout=sinks)):
        # get-default-sink returning nothing usable -> "default"
        assert gv.default_sink() == ("default", None)

    def run_default(cmd, **kw):
        if cmd[1:3] == ["list", "short"]:
            return subprocess.CompletedProcess(cmd, 0, sinks, "")
        return subprocess.CompletedProcess(cmd, 0, "alsa_output.usb-headset\n", "")

    with patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=run_default):
        assert gv.default_sink() == ("alsa_output.usb-headset", None)


# ---------------------------------------------------------------- audio monitor
@check
def monitor_is_a_separate_process_with_pulse_sink():
    cmd = gv.monitor_cmd("src", "alsa_output.speakers", False)
    assert cmd[0] == "ffmpeg" and "-c:v" not in cmd  # never the recorder
    assert cmd[cmd.index("-i") + 1] == "src" and cmd[-3:] == ["-f", "pulse",
                                                              "alsa_output.speakers"]
    assert cmd[cmd.index("-af") + 1] == gv.MONO_MAP
    stereo = gv.monitor_cmd("src", "sink", True)
    assert "-af" not in stereo  # stereo mode passes both channels through

    started = {}
    with patch(owner=gv.subprocess,
               Popen=lambda cmd, **kw: started.update(cmd=cmd, env=kw.get("env")) or None):
        assert gv.start_monitor("src", "sink.name", True) is None  # fake returns None
        # ffmpeg's pulse output ignores the URL device: routing needs PULSE_SINK
        assert started["env"]["PULSE_SINK"] == "sink.name"
        gv.start_monitor("src", "default", True)
        assert "PULSE_SINK" not in started["env"]  # let pulse pick its own default
    with patch(owner=gv.subprocess,
               Popen=lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            assert gv.start_monitor("src", "sink", True) is None
        assert "cannot start the audio monitor" in out.getvalue()


@check
def dead_monitor_warns_once_and_never_fails():
    class Mon:
        returncode = 3

        def poll(self):
            return 3

    out = io.StringIO()
    state = {"monitor": Mon()}
    with contextlib.redirect_stderr(out):
        gv.check_monitor(state)
        gv.check_monitor(state)  # second call must stay quiet
        gv.check_monitor({"monitor": None})  # no monitor configured
        class Running:
            def poll(self):
                return None

        gv.check_monitor({"monitor": Running()})  # still running
    text = out.getvalue()
    assert text.count("audio monitor stopped") == 1, text
    assert "recording continues" in text


@check
def audio_peak_and_duration_parsing():
    def run_with(stderr, stdout=""):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout, stderr)

    with patch(owner=gv.subprocess, run=run_with("max_volume: -24.1 dB\n")):
        assert gv.audio_peak("/tmp/x.mkv", 3) == -24.1
    with patch(owner=gv.subprocess, run=run_with("max_volume: -inf dB\n")):
        assert gv.audio_peak("/tmp/x.mkv", 3) == float("-inf")
    with patch(owner=gv.subprocess, run=run_with("no such stream\n")):
        assert gv.audio_peak("/tmp/x.mkv", 3) is None
    with patch(owner=gv.subprocess, run=run_with("", "12.5\n")):
        assert gv.media_duration("/tmp/x.mkv") == 12.5
    with patch(owner=gv.subprocess, run=run_with("", "N/A\n")):
        assert gv.media_duration("/tmp/x.mkv") is None


@check
def monitor_feedback_is_detected():
    """GV_SINK behind GV_AUDIO would howl: the monitor must be skipped."""
    sink = "alsa_output.speakers"
    assert gv.monitor_feedback(sink, f"{sink}.monitor") is True
    assert gv.monitor_feedback(sink, "alsa_input.usb-card.analog-stereo") is False
    assert gv.monitor_feedback("other-sink", f"{sink}.monitor") is False
    assert gv.monitor_feedback("default", f"{sink}.monitor") is False
    assert gv.monitor_feedback(None, f"{sink}.monitor") is False


@check
def gv_sink_override_is_honoured():
    sinks = "1\talsa_output.speakers\tx\n2\talsa_output.headset\tx\n"
    with patch(SINK="alsa_output.headset", owner=gv), \
         patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=fake_run(rc=0, stdout=sinks)):
        assert gv.default_sink() == ("alsa_output.headset", None)
    with patch(SINK="alsa_output.nope", owner=gv), \
         patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=fake_run(rc=0, stdout=sinks)):
        sink, note = gv.default_sink()
        assert sink is None and "GV_SINK=alsa_output.nope" in note
    # no pactl: pass the override through and let pulse resolve it
    with patch(SINK="alsa_output.headset", owner=gv), \
         patch(owner=gv.shutil, which=lambda _t: None):
        assert gv.default_sink() == ("alsa_output.headset", None)


# ---------------------------------------------------------------- preview
@check
def preview_free_uses_fuser():
    with patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=lambda *a, **k: subprocess.CompletedProcess(a, 0)):
        assert gv.preview_free("/dev/video0") is False  # 0 = something holds it
    with patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
         patch(owner=gv.subprocess, run=lambda *a, **k: subprocess.CompletedProcess(a, 1)):
        assert gv.preview_free("/dev/video0") is True
    with patch(owner=gv.shutil, which=lambda _t: None):
        assert gv.preview_free("/dev/video0") is True  # no fuser: assume free


class LiveFF:
    def poll(self):
        return None


class DeadFF:
    def __init__(self, rc=-9):
        self.rc = rc

    def poll(self):
        return self.rc


@check
def wait_preview_reports_geometry_or_gives_up():
    with patch(v4l2_fmt=lambda _d: "Width/Height : 720/576\nPixel Format: 'UYVY'\n"):
        assert gv.wait_preview(LiveFF()) == (720, 576)
        assert gv.wait_preview(DeadFF()) is None  # writer already gone
        with patch(v4l2_fmt=lambda _d: ""):
            assert gv.wait_preview(LiveFF(), timeout=0) is None
        with patch(v4l2_fmt=lambda _d: "UYVY but no geometry"):
            assert gv.wait_preview(LiveFF(), timeout=0) is None


class FakeProc:
    def __init__(self, rc, calls_before_exit=None):
        self.rc, self.signals = rc, []
        self.calls = calls_before_exit

    def wait(self, timeout=None):
        if self.calls is not None:
            self.calls -= 1
            if self.calls >= 0:
                raise subprocess.TimeoutExpired("ffplay", timeout)
        return self.rc

    def terminate(self):
        self.signals.append("TERM")

    def kill(self):
        self.signals.append("KILL")


@check
def play_semantics():
    state = {"stopping": False, "player": None}
    # ffplay dies immediately, twice: preview unavailable, keep recording
    with patch(owner=gv.subprocess, Popen=lambda *a, **k: FakeProc(-1)), \
         patch(owner=gv.time, sleep=lambda _s: None):
        assert gv.play((720, 576), "PAL", state, LiveFF()) is False
    # user closes the window cleanly: stop
    with patch(owner=gv.subprocess, Popen=lambda *a, **k: FakeProc(0)):
        assert gv.play((720, 576), "PAL", state, LiveFF()) is True
    # played a while, then failed: treat as closed, don't relaunch
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 5.0  # every call is 5s later: the player "ran for a while"
        return clock["t"]

    with patch(owner=gv.subprocess, Popen=lambda *a, **k: FakeProc(-1)), \
         patch(owner=gv.time, monotonic=tick, sleep=lambda _s: None):
        assert gv.play((720, 576), "PAL", state, LiveFF()) is True
    # ffmpeg died while the preview was up: stop, don't retry
    with patch(owner=gv.subprocess, Popen=lambda *a, **k: FakeProc(-1)):
        assert gv.play((720, 576), "PAL", state, DeadFF()) is True


@check
def wait_player_kills_wedged_preview():
    player = FakeProc(rc=-15, calls_before_exit=2)  # two timeouts, then terminate
    assert gv.wait_player(player, DeadFF(), {"stopping": False}) == -15
    assert player.signals and player.signals[0] == "TERM"
    # never exits even after TERM: escalated to KILL
    class Stubborn(FakeProc):
        def wait(self, timeout=None):
            if "KILL" in self.signals:
                return -9
            raise subprocess.TimeoutExpired("ffplay", timeout)

    stubborn = Stubborn(rc=-9)
    assert gv.wait_player(stubborn, DeadFF(), {"stopping": False}) == -9
    assert stubborn.signals == ["TERM", "KILL"]


@check
def finalize_forces_a_wedged_ffmpeg():
    class Wedged:
        """wait() times out until it is signalled; SIGKILL is the only thing that works."""

        def __init__(self, dies_on="TERM"):
            self.dies_on, self.signals = dies_on, []

        def poll(self):
            return -15 if self.dies_on in self.signals else None

        def wait(self, timeout=None):
            if self.dies_on in self.signals:
                return -15
            raise subprocess.TimeoutExpired("ffmpeg", timeout)

        def send_signal(self, sig):
            self.signals.append({gv.signal.SIGTERM: "TERM",
                                 gv.signal.SIGKILL: "KILL"}[sig])

    ff = Wedged(dies_on="TERM")
    assert gv.finalize(ff) == -15 and ff.signals == ["TERM"]
    stubborn = Wedged(dies_on="KILL")
    with patch(STOP_TIMEOUT=0):
        assert gv.finalize(stubborn) == -15
    assert stubborn.signals == ["TERM", "KILL"]

    # a clean stop needs no signals at all
    clean = FakeProc(0)
    clean.poll = lambda: 0
    clean.send_signal = lambda sig: clean.signals.append(sig)
    assert gv.finalize(clean) == 0 and clean.signals == []


@check
def desktop_env_fills_session_vars():
    saved = {k: os.environ.pop(k, None) for k in
             ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY",
              "SDL_VIDEODRIVER")}
    try:
        with patch(owner=gv.glob, glob=lambda p: ["/run/user/1000/xauth_ABC"]):
            gv.desktop_env()
        assert os.environ["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
        assert os.environ["DISPLAY"] == ":0"
        assert os.environ["SDL_VIDEODRIVER"] == "x11"
        assert os.environ["XAUTHORITY"].endswith("xauth_ABC")
        os.environ["DISPLAY"] = ":7"  # never override what the caller set
        gv.desktop_env()
        assert os.environ["DISPLAY"] == ":7"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------- env config
@check
def env_overrides_are_read_at_import():
    code = ("import importlib.util as u, os;"
            f"s=u.spec_from_file_location('m', {str(ROOT / 'gv-capture.py')!r});"
            "m=u.module_from_spec(s); s.loader.exec_module(m);"
            "print(m.VIDEO, m.PREVIEW, m.AUDIO)")
    env = dict(os.environ, GV_VIDEO="/dev/x", GV_PREVIEW="", GV_AUDIO="src")
    out = subprocess.run([sys.executable, "-c", code], env=env,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["/dev/x", "src"], out  # GV_PREVIEW= leaves PREVIEW empty


# ---------------------------------------------------------------- main()
class FakeFFProc:
    """Stands in for ffmpeg: creates the output file, then reports `rc`."""

    def __init__(self, out=None, rc=0, alive_polls=0):
        self.rc, self.signals, self.alive = rc, [], alive_polls
        self.returncode = rc
        if out:
            Path(out).write_bytes(b"x" * 100000)

    def terminate(self):
        self.signals.append("TERM")

    def kill(self):
        self.signals.append("KILL")

    def poll(self):
        if self.alive > 0:
            self.alive -= 1
            return None
        return self.rc

    def wait(self, timeout=None):
        return self.rc

    def send_signal(self, sig):
        self.signals.append(sig)


@check
def main_happy_path_finalizes():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td, "rec.mkv"))
        preview = Path(td, "video0")
        preview.write_bytes(b"")
        device = Path(td, "video3")
        device.write_bytes(b"")
        proc_holder = {}

        def popen(cmd, **kw):
            if "-c:v" in cmd:  # the recorder
                proc_holder["ff"] = FakeFFProc(out=out, rc=0, alive_polls=1)
                return proc_holder["ff"]
            if cmd[0] == "ffplay":
                return FakeProc(0)  # preview closed immediately
            return FakeFFProc(rc=0)  # audio monitor

        def run(cmd, **kw):
            # fuser: 1 = nothing holds the loopback; everything else succeeds
            return subprocess.CompletedProcess(
                cmd, 1 if cmd[0] == "fuser" else 0,
                "Width/Height : 720/576\nPixel Format: 'UYVY'\n", "")

        argv = sys.argv
        sys.argv = ["gv-capture.py", out]
        try:
            with patch(VIDEO=str(device), PREVIEW=str(preview), owner=gv), \
                 patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
                 patch(owner=gv.subprocess, Popen=popen, run=run), \
                 patch(owner=gv.time, sleep=lambda _s: None), \
                 patch(owner=gv, check_space=lambda _p: None,
                       audio_source=lambda: "src", default_sink=lambda: ("default", None),
                       media_duration=lambda _p: 6.0, audio_peak=lambda _p, _a: -20.0):
                printed = io.StringIO()
                with contextlib.redirect_stdout(printed):
                    gv.main()
                assert "done:" in printed.getvalue(), printed.getvalue()
        finally:
            sys.argv = argv
        assert Path(out).exists() and Path(out).stat().st_size > 0


@check
def main_reports_nothing_written():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td, "rec.mkv"))
        device = Path(td, "video3")
        device.write_bytes(b"")

        def popen(cmd, **kw):
            if "-c:v" in cmd:
                return FakeFFProc(out=None, rc=1)
            return FakeProc(0) if cmd[0] == "ffplay" else FakeFFProc(rc=0)

        argv = sys.argv
        sys.argv = ["gv-capture.py", out]
        try:
            with patch(VIDEO=str(device), PREVIEW="/dev/nope", owner=gv), \
                 patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
                 patch(owner=gv.subprocess, Popen=popen,
                       run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")), \
                 patch(owner=gv.time, sleep=lambda _s: None), \
                 patch(owner=gv, check_space=lambda _p: None,
                       audio_source=lambda: "src",
                       default_sink=lambda: ("default", None),
                       media_duration=lambda _p: 6.0, audio_peak=lambda _p, _a: -20.0):
                assert "nothing written" in expect_exit(1, gv.main)
        finally:
            sys.argv = argv


@check
def main_deletes_header_only_mkv():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td, "rec.mkv"))
        device = Path(td, "video3")
        device.write_bytes(b"")

        def popen(cmd, **kw):
            if "-c:v" in cmd:
                Path(out).write_bytes(b"tiny")  # header only, ffmpeg failed
                return FakeFFProc(out=None, rc=234)
            return FakeProc(0) if cmd[0] == "ffplay" else FakeFFProc(rc=0)

        argv = sys.argv
        sys.argv = ["gv-capture.py", out]
        try:
            with patch(VIDEO=str(device), PREVIEW="/dev/nope", owner=gv), \
                 patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t), \
                 patch(owner=gv.subprocess, Popen=popen,
                       run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")), \
                 patch(owner=gv.time, sleep=lambda _s: None), \
                 patch(owner=gv, check_space=lambda _p: None,
                       audio_source=lambda: "src",
                       default_sink=lambda: ("default", None),
                       media_duration=lambda _p: 6.0, audio_peak=lambda _p, _a: -20.0):
                assert "failed at startup" in expect_exit(1, gv.main)
            assert not Path(out).exists()
        finally:
            sys.argv = argv


@check
def main_refuses_existing_output():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td, "rec.mkv")
        out.write_bytes(b"mine")
        device = Path(td, "video3")
        device.write_bytes(b"")
        argv = sys.argv
        sys.argv = ["gv-capture.py", str(out)]
        try:
            with patch(VIDEO=str(device), PREVIEW="/dev/nope", owner=gv), \
                 patch(owner=gv.shutil, which=lambda t: "/usr/bin/" + t):
                assert "already exists" in expect_exit(1, gv.main)
            assert out.read_bytes() == b"mine"  # untouched
        finally:
            sys.argv = argv


@check
def script_compiles():
    subprocess.run([sys.executable, "-m", "py_compile", str(SPEC.origin)], check=True)


def main():
    failures = 0
    for fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        except SystemExit as e:  # a test that exits is a failing test
            failures += 1
            print(f"FAIL {fn.__name__}: unexpected SystemExit {e.code}")
        except Exception as e:  # noqa: BLE001 - a crashing test is a failing test
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} offline checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
