#!/usr/bin/env python3
"""Fake v4l2-ctl for testing cam-still without a camera (Linux/WSL).

Understands exactly what cam-still calls:
  -d DEV --get-ctrl=a,b,c           prints "a: N" from the state file
  -d DEV --set-ctrl=a=N,b=M         writes to the state and to the log
  -d DEV --set-fmt-video=width=W,height=H,pixelformat=NV12
  -d DEV --get-fmt-video            like rkisp_mainpath (Multiplanar, 1 plane)
  -d DEV --set-fmt-video=... --stream-mmap=4 --stream-skip=S --stream-count=N --stream-to=-
                                    S+N "<" characters to stderr, N frames to stdout;
                                    without --stream-count — endless (series mode)

Frames cycle from the file $FAKE_V4L2_FRAMES (NV12 W*H*3/2 back to back, e.g.
reference *.nv12 frames saved by a client), otherwise a pattern. State and call log go to
$FAKE_V4L2_STATE (json) and $FAKE_V4L2_LOG. $FAKE_V4L2_FAIL_AFTER=K — fail after
K delivered frames (interruption test). $FAKE_V4L2_FPS — frame rate.
"""
import json, os, sys, time

STATE = os.environ.get("FAKE_V4L2_STATE", "/tmp/fake_v4l2_state.json")
LOG = os.environ.get("FAKE_V4L2_LOG", "/tmp/fake_v4l2.log")


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"ctrl": {"exposure": 4095, "analogue_gain": 2816, "gain": 256}, "w": 3280, "h": 2464}


def save(st):
    with open(STATE, "w") as f:
        json.dump(st, f)


def journal(line):
    with open(LOG, "a") as f:
        f.write(line + "\n")


args = sys.argv[1:]
st = load()
opts = {}
for a in args:
    if a.startswith("--"):
        k, _, v = a[2:].partition("=")
        opts[k] = v
journal(" ".join(args))

if "set-fmt-video" in opts:
    kv = dict(p.split("=") for p in opts["set-fmt-video"].split(","))
    st["w"], st["h"] = int(kv["width"]), int(kv["height"])
    save(st)
if "set-ctrl" in opts:
    for p in opts["set-ctrl"].split(","):
        k, v = p.split("=")
        st["ctrl"][k] = int(v)
    save(st)
if "get-ctrl" in opts:
    for k in opts["get-ctrl"].split(","):
        print("%s: %d" % (k, st["ctrl"][k]))
if "get-fmt-video" in opts:
    w, h = st["w"], st["h"]
    print("Format Video Capture Multiplanar:\n\tWidth/Height      : %d/%d\n"
          "\tPixel Format      : 'NV12' (Y/UV 4:2:0)\n\tField             : None\n"
          "\tNumber of planes  : 1\n\tPlane 0           :\n"
          "\t   Bytes per Line : %d\n\t   Size Image     : %d" % (w, h, w, w * h * 3 // 2))
if "stream-mmap" in opts:
    w, h = st["w"], st["h"]
    fb = w * h * 3 // 2
    # without --stream-count — endless, like the real v4l2-ctl (cam-still 2 series mode)
    skip, count = int(opts.get("stream-skip", 0)), int(opts.get("stream-count", 0))
    count = count if count > 0 else 1 << 40
    src = os.environ.get("FAKE_V4L2_FRAMES")
    frames = []
    if src:
        data = open(src, "rb").read()
        frames = [data[i:i + fb] for i in range(0, len(data) - fb + 1, fb)]
    if not frames:
        frames = [bytes((x * 7 + k) & 0xFF for x in range(256)) * (fb // 256) + b"\0" * (fb % 256) for k in range(3)]
    fail = int(os.environ.get("FAKE_V4L2_FAIL_AFTER", "-1"))
    dt = 1.0 / float(os.environ.get("FAKE_V4L2_FPS", "50"))
    out = sys.stdout.buffer
    for i in range(skip + count):
        time.sleep(dt)
        sys.stderr.write("<")
        sys.stderr.flush()
        if i >= skip:
            k = i - skip
            if k == fail:
                sys.stderr.write("\nVIDIOC_DQBUF: fake failure\n")
                sys.exit(1)
            try:
                out.write(frames[k % len(frames)])
                out.flush()
            except BrokenPipeError:
                sys.exit(1)
    sys.stderr.write("\n")
