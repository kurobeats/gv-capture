#!/usr/bin/env python3
"""Self-check for gv-capture.py helpers: python3 tests/test_gv_capture.py"""
import contextlib
import importlib.util
import io
import subprocess
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "gv_capture", Path(__file__).resolve().parents[1] / "gv-capture.py")
gv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gv)


def expect_exit(code, fn, *a):
    """Run fn(*a); assert it exits with `code` and return what it printed."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            fn(*a)
    except SystemExit as e:
        assert e.code == code, (fn.__name__, e.code)
        return err.getvalue()
    raise AssertionError(f"{fn.__name__}{a} did not exit {code}")


# args: defaults, standard, mono/stereo, positional outfile
assert gv.parse_args([]) == (None, "PAL", False)
assert gv.parse_args(["/tmp/a.mkv"]) == ("/tmp/a.mkv", "PAL", False)
assert gv.parse_args(["/tmp/a.mkv", "ntsc", "STEREO"]) == ("/tmp/a.mkv", "NTSC", True)
assert gv.parse_args(["stereo", "/tmp/b.mkv", "PAL"]) == ("/tmp/b.mkv", "PAL", True)
for bad in (["/tmp/a.mkv", "/tmp/b.mkv"], ["PAL", "NTSC"], ["--nope"]):
    expect_exit(2, gv.parse_args, bad)  # ambiguous or unknown -> refuse, don't guess
assert "usage: gv-capture.py" in expect_exit(0, gv.parse_args, ["-h"])

# geometry from the real `v4l2-ctl --get-fmt-video` shape
dump = """Format Video Capture:
\tWidth/Height      : 720/576
\tPixel Format      : 'UYVY' (UYVY 4:2:2)
"""
assert gv.parse_geometry(dump) == (720, 576)
assert gv.parse_geometry("") is None and gv.parse_geometry("nonsense") is None

# profiles must be usable as-is by ffmpeg and consistent with their recorded size
for name, p in gv.PROFILES.items():
    w, h = (int(v) for v in p["size"].split("x"))
    rw, rh = (int(v) for v in p["recsize"].split("x"))
    assert float(p["fps"]) > 0 and p["vf"], p
    assert rw == w and 0 < rh <= h, p  # a crop may only trim rows
assert gv.PROFILES["PAL"]["recsize"] == "720x566"

# command construction: -maps, filters, mono only on the file output
cmd = gv.ffmpeg_cmd("/tmp/out.mkv", "PAL", False, gv.PROFILES["PAL"], "src", True)
assert cmd[cmd.index("-vf") + 1] == "setfield=tff,crop=720:566:0:8"
assert cmd.count("-map") == 4, cmd  # 2 for the file, 1 preview, 1 monitor
assert cmd[-3:] == ["-f", "pulse", "default"], cmd  # monitor last, with its sink
# mono mode: left channel at unity on the file output, and on the monitor too
assert cmd[cmd.index("-af") + 1] == gv.MONO_MAP
assert cmd.index("-af") < cmd.index("/tmp/out.mkv") < cmd.index(gv.PREVIEW)
assert cmd[cmd.index("-af", cmd.index(gv.PREVIEW)) + 1] == gv.MONO_MAP
assert cmd.index("/tmp/out.mkv") < cmd.index(gv.PREVIEW)
assert "-nostdin" in cmd and "-n" in cmd  # never clobber an existing mkv

no_prev = gv.ffmpeg_cmd("/tmp/o.mkv", "NTSC", True, gv.PROFILES["NTSC"], "src", False)
assert no_prev.count("-map") == 3 and gv.PREVIEW not in no_prev
stereo_cmd = gv.ffmpeg_cmd("/tmp/o.mkv", "PAL", True, gv.PROFILES["PAL"], "src", False)
assert "-af" not in stereo_cmd  # "stereo" passes both channels through untouched
# no sink -> no monitor output at all, but the recording still happens
no_mon = gv.ffmpeg_cmd("/tmp/o.mkv", "PAL", False, gv.PROFILES["PAL"], "src", False, None)
assert no_mon.count("-map") == 2 and no_mon.count("pulse") == 1  # input only, no monitor

# preflight must refuse rather than start a doomed capture
real_which, real_video = gv.shutil.which, gv.VIDEO
try:
    gv.shutil.which = lambda _t: None
    assert "not found" in expect_exit(1, gv.preflight, "/tmp/gv-test-ok.mkv")

    gv.shutil.which = lambda t: "/usr/bin/" + t
    gv.VIDEO = "/dev/video-does-not-exist"
    assert "missing" in expect_exit(1, gv.preflight, "/tmp/gv-test-ok.mkv")

    fake_dev = Path("/tmp/gv-test-video")
    fake_dev.write_bytes(b"")
    gv.VIDEO = str(fake_dev)
    tmp = Path("/tmp/gv-test-out.mkv")
    tmp.write_bytes(b"x")
    try:
        assert "already exists" in expect_exit(1, gv.preflight, str(tmp))
    finally:
        tmp.unlink()
        fake_dev.unlink()
finally:
    gv.shutil.which, gv.VIDEO = real_which, real_video

# audio source: keep the configured name when pactl agrees or is absent,
# pick another GV-USB2 source when the name changed, refuse when there is none
real_shutil_which, real_run = gv.shutil.which, gv.subprocess.run


class FakePactl:
    def __init__(self, names):
        self.names, self.returncode, self.stdout = names, 0, ""

    def __call__(self, cmd, **kw):
        self.stdout = "".join(f"{i}\t{n}\talsa\tx\tRUNNING\n"
                              for i, n in enumerate(self.names))
        return self


try:
    gv.shutil.which = lambda t: "/usr/bin/" + t
    gv.subprocess.run = FakePactl([gv.AUDIO, "alsa_input.usb-GV-USB2_2-02.analog-stereo"])
    assert gv.audio_source() == gv.AUDIO
    gv.subprocess.run = FakePactl(["alsa_input.usb-I-O_DATA_GV-USB2_9-02.stereo-fallback"])
    assert "GV-USB2_9-02" in gv.audio_source()
    gv.subprocess.run = FakePactl(["alsa_output.hdmi"])
    assert "no GV-USB2 audio source" in expect_exit(1, gv.audio_source)
    gv.shutil.which = lambda _t: None  # no pactl: trust the config
    assert gv.audio_source() == gv.AUDIO
finally:
    gv.shutil.which, gv.subprocess.run = real_shutil_which, real_run

# preview_free(): fuser exit status decides (0 = something holds the loopback)
real_run2 = gv.subprocess.run
try:
    gv.shutil.which = lambda t: "/usr/bin/" + t
    gv.subprocess.run = lambda *a, **kw: subprocess.CompletedProcess(a, 0)
    assert gv.preview_free("/dev/video0") is False
    gv.subprocess.run = lambda *a, **kw: subprocess.CompletedProcess(a, 1)
    assert gv.preview_free("/dev/video0") is True
    gv.shutil.which = lambda _t: None
    assert gv.preview_free("/dev/video0") is True  # no fuser: assume it is free
finally:
    gv.shutil.which, gv.subprocess.run = real_shutil_which, real_run2


# wait_player() must not hang on a preview that stays up after ffmpeg died
class Player:
    def __init__(self):
        self.killed = False

    def wait(self, timeout=None):
        if self.killed:
            return -15
        raise subprocess.TimeoutExpired("ffplay", timeout)

    def terminate(self):
        self.killed = True

    def kill(self):
        self.killed = True


class DeadFF:
    def poll(self):
        return -9


player = Player()
assert gv.wait_player(player, DeadFF(), {"stopping": False}) == -15
assert player.killed

# play(): a preview that never starts must not stop the capture (OBS may hold the
# loopback), while a closed window must stop it
class FakeProc:
    def __init__(self, rc):
        self.rc = rc

    def wait(self, timeout=None):
        return self.rc

    def terminate(self):
        pass

    def kill(self):
        pass


class LiveFF:
    def poll(self):
        return None


real_popen = gv.subprocess.Popen
try:
    # ffplay dies instantly, twice: preview unavailable, keep recording
    gv.subprocess.Popen = lambda *a, **kw: FakeProc(-1)
    assert gv.play((720, 576), "PAL", {"stopping": False, "player": None}, LiveFF()) is False
    # user closes the window: stop
    gv.subprocess.Popen = lambda *a, **kw: FakeProc(0)
    assert gv.play((720, 576), "PAL", {"stopping": False, "player": None}, LiveFF()) is True
finally:
    gv.subprocess.Popen = real_popen

# finalize() must force a wedged ffmpeg instead of hanging forever
class Wedged:
    returncode = -9

    def __init__(self):
        self.signals = []

    def poll(self):
        return self.returncode if len(self.signals) > 1 else None

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout=None):
        if len(self.signals) > 1:
            return self.returncode
        raise subprocess.TimeoutExpired("ffmpeg", timeout)


wedged = Wedged()
gv.STOP_TIMEOUT = 0
assert gv.finalize(wedged) == -9
assert wedged.signals[0] == gv.signal.SIGTERM, wedged.signals

subprocess.run([sys.executable, "-m", "py_compile", str(SPEC.origin)], check=True)
print("ok")
