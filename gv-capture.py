#!/usr/bin/env python3
"""GV-USB2 FFV1 lossless capture + live preview window + audio monitor.

usage: gv-capture.py [outfile.mkv] [PAL|NTSC] [stereo|mono]
  - default mixes audio to mono (GV-USB2 sound is single-channel); pass "stereo" for 2ch
  - stop = Ctrl-C, SIGTERM, SIGHUP or close the preview window; the mkv is finalized cleanly
  - outputs: 1) mkv file  2) raw UYVY -> v4l2loopback /dev/video0 (preview)  3) audio -> default sink
  - device overrides: GV_VIDEO, GV_PREVIEW, GV_AUDIO environment variables

exit: 0 = finalized and the mkv is non-empty, 1 = refused to start / capture failed,
      2 = bad usage
"""
import glob
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

VIDEO = os.environ.get("GV_VIDEO", "/dev/video3")
PREVIEW = os.environ["GV_PREVIEW"] if "GV_PREVIEW" in os.environ else "/dev/video0"
AUDIO = os.environ.get(
    "GV_AUDIO",
    "alsa_input.usb-I-O_DATA_I-O_DATA_GV-USB2_000000000000001-02.stereo-fallback")

PROFILES = {
    # PAL: rows 0-6 and 575 are blanked (TW9910 delivers 576, ~568 active)
    "PAL": {"size": "720x576", "fps": "25",
            "vf": "setfield=tff,crop=720:566:0:8", "recsize": "720x566"},
    "NTSC": {"size": "720x486", "fps": "29.97", "vf": "setfield=tff", "recsize": "720x486"},
}
STOP_TIMEOUT = 120  # seconds to let ffmpeg write the trailer before we force it
GRACEFUL_RC = (0, 130, 255, -signal.SIGINT, -signal.SIGTERM, -signal.SIGHUP)
MONO_MAP = "pan=mono|c0=c0"  # the only live channel; keeps full level

GEO_RE = re.compile(r"Width/Height\s*:\s*(\d+)/(\d+)")


def die(msg, code=1):
    print(f"gv-capture: {msg}", file=sys.stderr)
    sys.exit(code)


def usage(code=2):
    print(__doc__.strip() if code == 0 else "usage: gv-capture.py [outfile.mkv] "
          "[PAL|NTSC] [stereo|mono]", file=sys.stdout if code == 0 else sys.stderr)
    sys.exit(code)


def parse_args(argv):
    out, std, stereo, std_set = None, "PAL", False, False
    for a in argv:
        if a in ("-h", "--help"):
            usage(0)
        elif a.upper() in PROFILES:
            if std_set:
                usage()  # two standards: ambiguous, don't guess
            std, std_set = a.upper(), True
        elif a.lower() in ("stereo", "mono"):
            stereo = a.lower() == "stereo"
        elif a.startswith("-"):
            usage()
        elif out is None:
            out = a
        else:
            usage()
    return out, std, stereo


def parse_geometry(fmt_dump):
    """(width, height) from `v4l2-ctl --get-fmt-video` output, or None."""
    m = GEO_RE.search(fmt_dump)
    return (int(m.group(1)), int(m.group(2))) if m else None


def v4l2_fmt(dev):
    r = subprocess.run(["v4l2-ctl", "-d", dev, "--get-fmt-video"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def audio_source():
    """Configured source, or another GV-USB2 one, or fail with the list of sources."""
    if not shutil.which("pactl"):
        return AUDIO  # cannot verify without pactl; trust the config
    r = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True)
    if r.returncode != 0:
        return AUDIO
    names = [line.split()[1] for line in r.stdout.splitlines() if len(line.split()) > 1]
    if AUDIO in names or not names:
        return AUDIO
    gv = [n for n in names if "GV-USB2" in n.upper()]
    if gv:
        print(f"note: using audio source {gv[0]}", file=sys.stderr)
        return gv[0]
    die(f"no GV-USB2 audio source (set GV_AUDIO). available: {', '.join(names)}")


def default_sink():
    """(sink, note) for the audio monitor; sink is None when there is nowhere to play."""
    if not shutil.which("pactl"):
        return "default", None
    r = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True)
    if r.returncode != 0:
        return "default", None
    names = [line.split()[1] for line in r.stdout.splitlines() if len(line.split()) > 1]
    if not names:
        return None, "no audio sink found; audio monitor disabled"
    d = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True)
    sink = d.stdout.strip() if d.returncode == 0 else ""
    return (sink if sink in names else "default"), None


def check_space(path):
    p = Path(path).parent
    while not p.exists() and p != p.parent:
        p = p.parent
    free = shutil.disk_usage(p).free / 2 ** 30
    if free < 1:
        die(f"only {free:.1f} GiB free on {p}; FFV1 4:2:2 needs tens of GiB per hour")
    if free < 20:
        print(f"warning: only {free:.1f} GiB free on {p}; FFV1 4:2:2 uses tens of GiB/hour",
              file=sys.stderr)


def preflight(out):
    for tool in ("ffmpeg", "v4l2-ctl"):
        if not shutil.which(tool):
            die(f"{tool} not found in PATH")
    if not os.path.exists(VIDEO):
        die(f"capture device {VIDEO} missing (set GV_VIDEO)")
    if Path(out).exists():
        die(f"{out} already exists; move it or pass another name")
    check_space(out)


def desktop_env():
    # so the preview window lands on the KDE session even when launched via ssh
    rd = os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault("WAYLAND_DISPLAY", "wayland-1")
    os.environ.setdefault("DISPLAY", ":0")
    if not os.environ.get("XAUTHORITY"):
        xs = sorted(glob.glob(f"{rd}/xauth_*"))
        if xs:
            os.environ["XAUTHORITY"] = xs[0]
    os.environ.setdefault("SDL_VIDEODRIVER", "x11")  # SDL here has no wayland backend


def ffmpeg_cmd(out, std, stereo, prof, src, preview, monitor="default"):
    ff_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-n",
        "-f", "v4l2", "-video_size", prof["size"], "-pix_fmt", "uyvy422", "-i", VIDEO,
        "-f", "pulse", "-i", src,
        "-vf", prof["vf"],
        # output 1: file
        "-map", "0:v", "-map", "1:a",
        "-c:v", "ffv1", "-level", "3", "-threads", "4", "-pix_fmt", "yuv422p",
        "-c:a", "aac", "-b:a", "256k",
    ]
    if not stereo:  # mono source on front-left: average-downmixing just loses 6 dB
        ff_cmd += ["-af", MONO_MAP]
    ff_cmd += ["-f", "matroska", out]
    if preview:  # output 2: loopback preview (unfiltered, i.e. full frame geometry)
        ff_cmd += ["-map", "0:v", "-c:v", "rawvideo", "-pix_fmt", "uyvy422",
                   "-f", "v4l2", PREVIEW]
    # output 3: audio monitor. The sink name is not optional: "-f pulse" without a
    # filename is dropped by ffmpeg as trailing options and nothing plays.
    if monitor:
        ff_cmd += ["-map", "1:a"] + ([] if stereo else ["-af", MONO_MAP])
        ff_cmd += ["-c:a", "pcm_s16le", "-f", "pulse", monitor]
    return ff_cmd


def preview_free(dev):
    """False when another process holds the loopback (OBS virtual camera, a player).

    ffmpeg dies outright if it cannot claim the loopback, so check before using it.
    """
    if not shutil.which("fuser"):
        return True
    return subprocess.run(["fuser", "-s", dev], capture_output=True).returncode != 0


def wait_preview(ff, timeout=10.0):
    """Device geometry once the ffmpeg writer has claimed the loopback, else None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ff.poll() is not None:
            return None
        dump = v4l2_fmt(PREVIEW)
        if "UYVY" in dump.upper():
            return parse_geometry(dump)
        time.sleep(0.25)
    return None


def wait_player(p, ff, state):
    """Wait for ffplay, but put it down if it wedges (no writer left, or stopping).

    A dead ffmpeg leaves ffplay blocked on the loopback rather than exiting: its
    window stays up frozen and we would never finalize the mkv.
    """
    while True:
        try:
            return p.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        except KeyboardInterrupt:
            return None
        if ff.poll() is not None or state["stopping"]:
            p.terminate()
            try:
                return p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                return p.wait()


def play(geo, std, state, ff):
    """Run ffplay until it exits; one retry if it raced the writer.

    Returns True when the capture should stop with it (window closed, or we were
    signalled), False when the preview never came up at all.
    """
    gw, gh = geo
    cmd = ["ffplay", "-hide_banner", "-loglevel", "error",
           "-fflags", "nobuffer", "-framedrop",
           "-window_title", f"GV-USB2 monitor ({std})",
           "-f", "v4l2", "-pixel_format", "uyvy422",
           "-video_size", f"{gw}x{gh}", "-i", PREVIEW]
    for _ in (1, 2):
        started = time.monotonic()
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        state["player"] = p
        try:
            rc = wait_player(p, ff, state)
        finally:
            state["player"] = None
        if rc in (0, None) or state["stopping"] or ff.poll() is not None:
            return True
        if time.monotonic() - started > 2:
            return True  # it played for a while: the user closed it, don't argue
        time.sleep(0.5)  # it died right away: probably raced the writer, retry once
    return False  # never got a preview


def finalize(ff):
    """Wait for ffmpeg, forcing termination if it wedges; returns its exit code."""
    try:
        return ff.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("warning: ffmpeg did not stop; forcing it", file=sys.stderr)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if ff.poll() is not None:
            break
        ff.send_signal(sig)
        try:
            return ff.wait(timeout=10)
        except subprocess.TimeoutExpired:
            continue
    return ff.wait()


def main():
    out, std, stereo = parse_args(sys.argv[1:])
    prof = PROFILES[std]
    w, h = prof["size"].split("x")
    if out is None:
        out = str(Path.home() / "Videos" / f"gv-{time.strftime('%Y%m%d-%H%M%S')}.mkv")

    desktop_env()  # before any pactl probe: over ssh PulseAudio needs the session env
    preflight(out)
    src = audio_source()
    monitor, monitor_note = default_sink()
    if monitor_note:
        print(f"note: {monitor_note}", file=sys.stderr)

    try:
        r = subprocess.run(["v4l2-ctl", "-d", VIDEO, "--set-standard", std],
                           capture_output=True, text=True)
    except OSError as e:
        die(f"cannot run v4l2-ctl: {e}")
    if r.returncode != 0:
        die(f"cannot set {std} on {VIDEO}: {(r.stderr or r.stdout).strip()}")
    # re-set format after standard: driver otherwise hands out short buffers (gvusb2 quirk)
    subprocess.run(["v4l2-ctl", "-d", VIDEO,
                    "--set-fmt-video", f"width={w},height={h},pixelformat=UYVY"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    preview = bool(PREVIEW) and os.path.exists(PREVIEW)
    if preview and not shutil.which("ffplay"):
        print("warning: ffplay not found; recording without a preview window", file=sys.stderr)
        preview = False  # also skips the rawvideo loopback output, which is not free
    elif preview and not preview_free(PREVIEW):
        print(f"warning: {PREVIEW} is in use (OBS virtual camera?); "
              f"recording without a preview", file=sys.stderr)
        preview = False

    print(f"recording -> {out} ({prof['recsize']} @ {prof['fps']}, FFV1 lossless 4:2:2, "
          f"audio {'stereo' if stereo else 'mono'}"
          f"{', monitor -> ' + monitor if monitor else ''})")
    if not preview:
        print(f"note: no preview device at {PREVIEW or '(unset)'}; recording only")
    print("Ctrl-C or close the preview window to stop")
    sys.stdout.flush()

    state = {"stopping": False, "signalled": False, "player": None, "ff": None}

    def on_signal(_sig=None, _frm=None):
        state["signalled"] = True
        stop()

    def stop():
        # exactly ONE SIGINT to ffmpeg, ever — a second one aborts instead of finalizing
        if state["stopping"]:
            return
        state["stopping"] = True
        player = state["player"]
        if player is not None and player.poll() is None:
            player.terminate()  # a SIGTERM must not leave the preview blocking us
        ff = state["ff"]
        if ff is not None and ff.poll() is None:
            ff.send_signal(signal.SIGINT)

    # installed before ffmpeg starts: a Ctrl-C landing in between must not leave an
    # orphaned ffmpeg recording in its own process group until the disk fills up
    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, on_signal)

    try:
        # own pgrp: terminal SIGINT must not reach ffmpeg directly — the handler
        # above sends it exactly one SIGINT (two abort the trailer instead of
        # finalizing the mkv); ffplay stays in this group so Ctrl-C closes it
        ff = state["ff"] = subprocess.Popen(
            ffmpeg_cmd(out, std, stereo, prof, src, preview, monitor),
            preexec_fn=os.setpgrp, stdin=subprocess.DEVNULL)
    except OSError as e:
        die(f"cannot start ffmpeg: {e}")

    try:
        if not preview:
            if ff.poll() is None:
                while not state["stopping"] and ff.poll() is None:
                    time.sleep(0.2)
        else:
            geo = wait_preview(ff)
            if ff.poll() is None:  # ffmpeg died: no point playing, report below
                if geo is None:
                    print(f"warning: preview not ready on {PREVIEW}; recording continues",
                          file=sys.stderr)
                shown = play(geo or tuple(int(v) for v in prof["size"].split("x")),
                             std, state, ff)
                if not shown and ff.poll() is None and not state["stopping"]:
                    print(f"warning: preview failed on {PREVIEW}; recording continues",
                          file=sys.stderr)
                if ff.poll() is None:
                    while not state["stopping"] and ff.poll() is None:
                        time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop()
        rc = finalize(ff)

    size = Path(out).stat().st_size if Path(out).exists() else 0
    if not size:
        hint = f"; if OBS holds {PREVIEW}, stop its virtual camera" if preview else ""
        die(f"nothing written to {out} (ffmpeg exit {rc}){hint}. "
            f"see ffmpeg errors above")
    if size < 2 ** 16 and rc not in GRACEFUL_RC and not state["signalled"]:
        Path(out).unlink(missing_ok=True)  # header-only mkv from a failed start
        die(f"ffmpeg failed at startup (exit {rc}); nothing usable written. "
            f"see ffmpeg errors above")
    if rc not in GRACEFUL_RC and not state["signalled"]:
        die(f"ffmpeg exited unexpectedly ({rc}); {out} ({size / 2 ** 30:.2f} GiB) "
            f"may be truncated")
    print(f"done: {out} ({size / 2 ** 30:.2f} GiB)")


if __name__ == "__main__":
    main()
