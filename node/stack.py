#!/usr/bin/env python3
"""Stacks N frames of the RTSP stream by maximum (lighten) — so that one shot
collects the whole outline that a moving light spot traces faster than a camera frame.

Frames are pulled as raw RGB through a pipe from gst-launch: there is no PIL on the board, and the
software decoder avdec_h264 is mandatory (mpph264dec pulls in RGA, see docs/radxa-zero3.md).
The result is written as raw RGB; JPEG encoding is a separate gstreamer call.
"""
import subprocess, sys
import numpy as np

W, H = 1920, 1088
N = int(sys.argv[1]) if len(sys.argv) > 1 else 120
SKIP = 3  # the first frames after connecting to RTSP arrive corrupted

cmd = [
    "gst-launch-1.0", "-q",
    "rtspsrc", "location=rtsp://127.0.0.1:8554/cam", "protocols=tcp", "latency=100",
    "!", "rtph264depay", "!", "h264parse", "!", "avdec_h264",
    "!", "videoconvert", "!", "video/x-raw,format=RGB",
    "!", "fdsink", "fd=1",
]
p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

sz = W * H * 3
acc = None
last = None
got = 0
try:
    while got < N + SKIP:
        buf = p.stdout.read(sz)
        if len(buf) < sz:
            break
        got += 1
        if got <= SKIP:
            continue
        a = np.frombuffer(buf, dtype=np.uint8)
        last = a
        acc = a.copy() if acc is None else np.maximum(acc, a)
finally:
    p.kill()

if acc is None:
    sys.exit("no frames")
open("/tmp/acc.rgb", "wb").write(acc.tobytes())
open("/tmp/one.rgb", "wb").write(last.tobytes())
print("frames stacked:", got - SKIP)
