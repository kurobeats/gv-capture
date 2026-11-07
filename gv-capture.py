#!/usr/bin/env python3
"""GV-USB2 FFV1 lossless capture + live preview window + audio monitor.

usage: gv-capture.py [outfile.mkv] [PAL|NTSC] [stereo]
  - default mixes audio to mono (GV-USB2 sound is single-channel); pass "stereo" for 2ch
  - stop = Ctrl-C or close the preview window; the mkv is finalized cleanly
  - outputs: 1) mkv file  2) raw UYVY -> v4l2loopback /dev/video0 (preview)  3) audio -> default sink
"""
import glob
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

VIDEO = "/dev/video3"
PREVIEW = "/dev/video0"  # v4l2loopback (OBS Virtual Camera)
AUDIO = "alsa_input.usb-I-O_DATA_I-O_DATA_GV-USB2_000000000000001-02.stereo-fallback"


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


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else str(
        Path.home() / "Videos" / f"gv-{time.strftime('%Y%m%d-%H%M%S')}.mkv")
    std = (sys.argv[2] if len(sys.argv) > 2 else "PAL").upper()
    stereo = len(sys.argv) > 3 and sys.argv[3] == "stereo"

    if std == "NTSC":
        size, fps, vfin, psize = "720x486", "29.97", "setfield=tff", "720x486"
    else:  # PAL: rows 0-6 and 575 are blanked (TW9910 delivers 576, ~568 active)
        size, fps = "720x576", "25"
        vfin, psize = "setfield=tff,crop=720:566:0:8", "720x566"
    w, h = size.split("x")

    subprocess.run(["v4l2-ctl", "-d", VIDEO, "--set-standard", std], check=True)
    # re-set format after standard: driver otherwise hands out short buffers (gvusb2 quirk)
    subprocess.run(["v4l2-ctl", "-d", VIDEO,
                    "--set-fmt-video", f"width={w},height={h},pixelformat=UYVY"],
                   stdout=subprocess.DEVNULL)
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    print(f"recording -> {out} ({psize} @ {fps}, FFV1 lossless 4:2:2, "
          f"audio {'stereo' if stereo else 'mono'})")
    print("Ctrl-C or close the preview window to stop")
    sys.stdout.flush()

    desktop_env()
    ff_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "v4l2", "-video_size", size, "-pix_fmt", "uyvy422", "-i", VIDEO,
        "-f", "pulse", "-i", AUDIO,
        "-vf", vfin,
        # output 1: file
        "-map", "0:v", "-map", "1:a",
        "-c:v", "ffv1", "-level", "3", "-threads", "4", "-pix_fmt", "yuv422p",
        "-c:a", "aac", "-b:a", "256k",
        *(["-ac", "1"] if not stereo else []),
        "-f", "matroska", out,
        # output 2: loopback preview
        "-map", "0:v", "-c:v", "rawvideo", "-pix_fmt", "uyvy422",
        "-f", "v4l2", PREVIEW,
        # output 3: audio monitor to default sink
        "-map", "1:a", "-c:a", "pcm_s16le", "-f", "pulse",
    ]
    ff = subprocess.Popen(ff_cmd)

    killed = {"flag": False}

    def stop(_sig=None, _frm=None):
        # exactly ONE SIGINT to ffmpeg, ever — a second one aborts instead of finalizing
        if not killed["flag"]:
            killed["flag"] = True
            ff.send_signal(signal.SIGINT)

    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, stop)

    # loopback only becomes openable once the ffmpeg writer has set its format
    for _ in range(40):
        if ff.poll() is not None:
            break
        r = subprocess.run(["v4l2-ctl", "-d", PREVIEW, "--get-fmt-video"],
                           capture_output=True, text=True)
        if "UYVY" in r.stdout:
            break
        time.sleep(0.25)

    # ffplay may race the writer; allow one retry, then let it be
    try:
        rc = subprocess.run(
            ["ffplay", "-hide_banner", "-loglevel", "error",
             "-fflags", "nobuffer", "-framedrop",
             "-window_title", f"GV-USB2 monitor ({std})",
             "-f", "v4l2", "-pixel_format", "uyvy422", "-video_size", psize,
             "-i", PREVIEW]).returncode
        if rc != 0 and ff.poll() is None and not killed["flag"]:
            time.sleep(0.5)
            subprocess.run(["ffplay", "-hide_banner", "-loglevel", "error",
                            "-fflags", "nobuffer", "-framedrop",
                            "-window_title", f"GV-USB2 monitor ({std})",
                            "-f", "v4l2", "-pixel_format", "uyvy422",
                            "-video_size", psize, "-i", PREVIEW])
    except KeyboardInterrupt:
        pass
    finally:
        stop()

    ff.wait()
    print(f"done: {out}")


if __name__ == "__main__":
    main()
