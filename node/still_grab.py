#!/usr/bin/env python3
"""On-board half of the measurement still: raw NV12 frames from the ISP main path
(/dev/video0) — as is, unprocessed — to the workstation.

Started by host/still.py (on the PC) over ssh for the duration of one still;
not needed by hand. Standard library only: numpy is not needed on the board.

v4l2-ctl itself writes the frames straight into the socket (or stdout); Python does not
pass them through: this way the sensor's 12.9 fps is limited only by the network (~110 MB/s, 9 fps of
full frames), whereas pumping through Python gave ~90 MB/s. gst-launch + fdsink manages
only ~8.6 fps even without the network.

Order: start capture -> --pre frames (pipeline is surely running) -> sensor
controls "neighbour value, then the target" (pitfall in docs/radxa-zero3.md:
v4l2 does not write a value equal to the cached one to the register) -> another --post frames
are dropped while the controls take effect -> N frames are delivered. Frames are counted by
the "<" characters v4l2-ctl prints to stderr for every captured frame,
including those skipped via --stream-skip.

Transport:
  --port P  listen on TCP (0 — any free port), print to stdout
            "READY <port>", wait for one connection from the --peer address for at most
            --accept-timeout seconds; the port is closed right after the connection;
  no --port  frames go to stdout (ssh transport), messages to stderr.
"""
import argparse, socket, subprocess, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--width", type=int, default=3280)
ap.add_argument("--height", type=int, default=2464)
ap.add_argument("-n", type=int, default=16)
ap.add_argument("--exposure", type=int, default=4095)
ap.add_argument("--again", type=int, default=2816)
ap.add_argument("--dgain", type=int, default=256)
ap.add_argument("--pre", type=int, default=1, help="frames before writing controls")
ap.add_argument("--post", type=int, default=4, help="frames after writing controls, dropped")
ap.add_argument("--port", type=int)
ap.add_argument("--peer", default="", help="accept a connection only from this address")
ap.add_argument("--accept-timeout", type=float, default=15.0)
a = ap.parse_args()

DEV = "/dev/video0"
W, H = a.width, a.height
if W % 16 or H % 16:
    sys.exit("dimensions must be multiples of 16 (see docs/radxa-zero3.md, pitfalls about RGA)")
log = sys.stdout if a.port is not None else sys.stderr


def say(*m):
    print(*m, file=log, flush=True)


def setctl(e, ag, dg):
    subprocess.run(["v4l2-ctl", "-d", DEV, f"--set-ctrl=exposure={e},analogue_gain={ag},gain={dg}"],
                   check=True, stdout=sys.stderr)


out = None
if a.port is not None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", a.port))
    srv.listen(1)
    say(f"READY {srv.getsockname()[1]}")
    deadline = time.time() + a.accept_timeout
    while True:
        srv.settimeout(max(0.1, deadline - time.time()))
        try:
            out, (ip, _) = srv.accept()
        except socket.timeout:
            sys.exit("the workstation did not connect")
        if not a.peer or ip == a.peer:
            break
        out.close()   # foreign address — keep waiting for ours
    srv.close()       # the port is open only until the connection
    out.setblocking(True)   # the fd goes to v4l2-ctl, which needs an ordinary blocking socket
    out.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)

t0 = time.time()
cmd = ["v4l2-ctl", "-d", DEV, f"--set-fmt-video=width={W},height={H},pixelformat=NV12",
       "--stream-mmap=4", f"--stream-skip={a.pre + a.post}", f"--stream-count={a.n}", "--stream-to=-"]
src = subprocess.Popen(cmd, stdout=out.fileno() if out else None, stderr=subprocess.PIPE, bufsize=0)
if out:
    out.close()   # v4l2-ctl has its own copy of the descriptor; the PC sees EOF when it exits
err = bytearray()
seen = 0
t_first = t_ctl = t_data = None
try:
    while True:
        c = src.stderr.read(1)
        if not c:
            break
        if c != b"<":
            err += c
            continue
        seen += 1
        if seen == a.pre:
            t_first = time.time()
            setctl(a.exposure - 1, a.again - 1, a.dgain + 1)
            setctl(a.exposure, a.again, a.dgain)
            t_ctl = time.time()
        if seen == a.pre + a.post + 1:
            t_data = time.time()
    rc = src.wait()
finally:
    if src.poll() is None:
        src.kill()
        src.wait()
t_end = time.time()
got = max(0, seen - a.pre - a.post)
if rc != 0 or got < a.n:
    # both gst (device=/dev/video0) and v4l2-ctl (-d /dev/video0); we ourselves do not appear in this list
    who = subprocess.run(["pgrep", "-af", DEV], capture_output=True, text=True).stdout.strip()
    msg = f"main path {DEV} delivered {got} of {a.n} frames {W}x{H} (v4l2-ctl, code {rc})"
    if who:
        msg += (f"\n  device busy: {who[:160]}"
                f"\n  the stream must run on the self path (DEVICE=/dev/video1 in "
                f"/usr/local/etc/cam-stream.conf, width at most 1920)")
    e = err.decode(errors="replace").strip()
    if e:
        msg += "\n  v4l2-ctl: " + e[-600:]
    print(msg, file=sys.stderr, flush=True)
    sys.exit(1)
fps = (a.n - 1) / (t_end - t_data) if a.n > 1 and t_data else 0.0
say(f"GRAB first frame {t_first - t0:.2f} s, controls {t_ctl - t_first:.2f} s, "
    f"warm-up {t_data - t0:.2f} s, frames {t_end - t_data:.2f} s ({fps:.1f} fps)")
