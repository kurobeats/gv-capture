# gv-capture

Captures the I-O DATA GV-USB2 (Linux gvusb2 driver) to FFV1 lossless 4:2:2 mkv
with a live preview window and audio monitor, on a PipeWire/KDE desktop.

## Usage (on the capture host)

    python3 gv-capture.py [outfile.mkv] [PAL|NTSC] [stereo]

- default output: `~/Videos/gv-<timestamp>.mkv`
- audio is mixed to mono by default (the GV-USB2 sound chip is single-channel);
  pass `stereo` to keep it as-is
- stop with Ctrl-C or by closing the preview window — the mkv is finalized cleanly
- preview goes through a v4l2loopback device (/dev/video0, "OBS Virtual Camera")
- PAL recordings are cropped 720x576 -> 720x566 (rows 0-6 and 575 are always blanked)

## Requirements

gvusb2 kernel driver, ffmpeg (ffv1), v4l-utils, ffplay (with SDL/x11),
PipeWire (audio capture + monitor).
