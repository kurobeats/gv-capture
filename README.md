# gv-capture

Captures the I-O DATA GV-USB2 (Linux gvusb2 driver) to FFV1 lossless 4:2:2 mkv
with a live preview window and audio monitor, on a PipeWire/KDE desktop.

## Usage (on the capture host)

    python3 gv-capture.py [outfile.mkv] [PAL|NTSC] [stereo|mono]

- default output: `~/Videos/gv-<timestamp>.mkv`
- audio is mixed down to mono by default; pass `stereo` to keep both channels
- stop with Ctrl-C, `kill`, or by closing the preview window — the mkv is finalized cleanly
- preview goes through a v4l2loopback device (/dev/video0, "OBS Virtual Camera"), shown
  uncropped at the device's own geometry (720x576 PAL / 720x486 NTSC)
- PAL recordings are cropped 720x576 -> 720x566 (rows 0-6 and 575 are always blanked)
- if the loopback is already in use (OBS virtual camera), or ffplay is missing, the
  preview is skipped with a warning and the recording still happens
- an existing output file is never overwritten: the run refuses to start instead
- exit status: `0` finalized, `1` refused to start or capture failed/truncated,
  `2` bad usage

Device overrides, when the names below move (udev renumbering, a different card):

    GV_VIDEO=/dev/video3     # capture device (gvusb2)
    GV_PREVIEW=/dev/video0   # v4l2loopback preview sink, empty to disable
    GV_AUDIO=alsa_input...   # pulse source; falls back to another "GV-USB2" source

## Audio

The GV-USB2 sound chip is single-channel and lands on the **front-left** channel of a
stereo pulse stream, with the right channel at the noise floor. So:

- mono mode takes front-left at unity gain (an average downmix would cost 6 dB)
- the monitor is fed mono in mono mode, so both speakers play it

## Requirements

gvusb2 kernel driver, ffmpeg (ffv1), v4l-utils, ffplay (with SDL/x11),
PipeWire (audio capture + monitor), psmisc (`fuser`, to spot a busy loopback —
optional, without it a busy loopback makes ffmpeg fail instead of skipping the preview).

## Checks

    python3 tests/test_gv_capture.py   # argument/geometry/command/preflight self-check
