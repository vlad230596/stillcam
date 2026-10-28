#!/usr/bin/env python3
"""Measurement still: raw frames straight from the ISP (no H.264), averaged over N frames.

Runs on the WORKSTATION (usually via `snap.ps1 -Still`). The camera board
only delivers raw NV12 frames from the ISP main path (/dev/video0) — that is
done by still_grab.py, which is shipped to the board over ssh for the duration of one still.
Averaging, colour and PNG happen here: on the Cortex-A55 the numpy processing and
zlib alone took ~20 s out of 24, on the PC about a second.

The live stream runs in parallel on the self path (/dev/video1) and is not stopped —
the ISP has two independent outputs from one sensor frame. The codec is not involved
at all, so neither mpp nor RGA is touched.

Sensor exposure and gain are shared by both paths. The values set for the still
intentionally remain after it: the live stream shows which
parameters the still was taken with (at exposure > ~2500 the stream drops to ~12.9 fps).

Frame transport (docs/radxa-zero3.md, "Measurement still"):
  tcp  the board listens on a random port only for the duration of the still and accepts one
       connection from this machine's address; ~110 MB/s, limited by gigabit;
  ssh  frames go over ssh stdout (aes128-gcm) — ~70 MB/s, limited by the board CPU;
       fallback if the TCP port does not get through.

Why this way and not from RTSP:
  * H.264 at 6 Mbit/s eats all fine detail — thin low-contrast lines in dark areas, sharp edges;
  * the full sensor frame 3280x2464 gives ~1.7x more pixels per mm on the object
    than the working stream 1920x1088 (which is also cropped vertically);
  * averaging N frames reduces noise by sqrt(N) — the scene is static, so it is free.

Writes:
  <out>-y16.png  luma, 16 bit, black level subtracted, no stretch — for measurements
  <out>.png      colour, 8 bit: white balance from a neutral grey surface + stretch — for the eye
  <out>.txt      capture parameters and statistics

Needs numpy (the workstation's system Python). PNG is written by hand via zlib.
"""
import argparse, base64, os, shlex, socket, struct, subprocess, sys, threading, time, zlib
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OPENSSH = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH")

ap = argparse.ArgumentParser()
ap.add_argument("--width", type=int, default=3280)
ap.add_argument("--height", type=int, default=2464)
ap.add_argument("-n", type=int, default=16, help="number of frames to average")
ap.add_argument("--exposure", type=int, default=4095, help="lines, 0..4095; above ~2500 the sensor lengthens the frame")
ap.add_argument("--again", type=int, default=2816, help="analogue gain, 256..2816 (x1..x11)")
ap.add_argument("--dgain", type=int, default=256, help="digital gain, 256 = x1")
# White balance measured on the table 2026-09-23 (R 88 / G 149 / B 119 with 3A off).
ap.add_argument("--wb", default="1.68,1,1.25", help="R,G,B multipliers")
ap.add_argument("--black", type=float, default=16.0, help="Y black level (NV12 from the ISP is limited range)")
ap.add_argument("--stretch", type=float, default=99.5, help="luma percentile that becomes white in the colour PNG")
ap.add_argument("--out", default="still", help="output file prefix on this machine")
ap.add_argument("--reduce", choices=["mean", "max"], default="mean",
                help="max — maximum stacking: trail of a moving light spot")
# Measured 2026-09-23 (gain 1400 -> 2816): while the board dequeues frames immediately,
# the first frame after the write is intermediate, the second already has the new values; with
# a full v4l2 queue (4 buffers) — the fifth. 4 leaves margin for the second case.
ap.add_argument("--post", type=int, default=4, help="frames to drop after writing controls")
ap.add_argument("--transport", choices=["tcp", "ssh"], default="tcp")
ap.add_argument("--cipher", default="aes128-gcm@openssh.com", help="ssh cipher for --transport ssh")
ap.add_argument("--png-level", type=int, default=1, help="zlib level; 1 is three times faster than 6 at +10%% size")
ap.add_argument("--host", default=os.environ.get("RADXA_HOST"),
                help="camera address; default — the RADXA_HOST environment variable (required if it is not set)")
ap.add_argument("--key", default=os.path.expandvars(r"%USERPROFILE%\.ssh\radxa_cam"))
a = ap.parse_args()
if not a.host:
    ap.error("camera address: --host or the RADXA_HOST environment variable")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

W, H = a.width, a.height
if W % 16 or H % 16:
    sys.exit("dimensions must be multiples of 16 (see docs/radxa-zero3.md, pitfalls about RGA)")
SZ = W * H * 3 // 2
T0 = time.perf_counter()
stages = []


def stage(name):
    stages.append((name, time.perf_counter()))


# --- on-board part ----------------------------------------------------------
# Only the Windows system OpenSSH: Git's ssh breaks pipelines inside the
# command argument.
ssh_exe = os.path.join(OPENSSH, "ssh.exe") if os.name == "nt" else "ssh"
grab_b64 = base64.b64encode(open(os.path.join(HERE, "..", "node", "still_grab.py"), "rb").read()).decode()
gargs = (f"-n {a.n} --width {W} --height {H} --exposure {a.exposure} --again {a.again} "
         f"--dgain {a.dgain} --post {a.post}")
if a.transport == "tcp":
    gargs += ' --port 0 --peer "${SSH_CLIENT%% *}"'
# In the ssh transport stdout carries the frames, so everything auxiliary goes to stderr.
# The board's sudo password is not stored in the repository: it comes from the environment
# variable RADXA_SUDO_PASS (docs/radxa-zero3.md, "Access"). Needed only if the stream sits on
# /dev/video0 and has to be stopped; without the variable the still fails in that case.
SUDO_PASS = os.environ.get("RADXA_SUDO_PASS", "")
if SUDO_PASS:
    sudo_fn = f"SUDO() {{ printf '%s\\n' {shlex.quote(SUDO_PASS)} | sudo -S -p '' \"$@\"; }}"
else:
    sudo_fn = ('SUDO() { echo "environment variable RADXA_SUDO_PASS is not set — sudo password '
               'of user radxa on the camera (docs/radxa-zero3.md, section Access)" >&2; exit 1; }')
board = f"""
{sudo_fn}
if systemctl is-active --quiet rkaiq_3A; then echo "auto exposure is on: run snap.ps1 -ManualExposure first" >&2; exit 1; fi
main=0; pgrep -f 'v4l2src device=/dev/video0 ' >/dev/null && main=1
[ $main = 1 ] && {{ echo "stream runs on main path /dev/video0 — stopping it for the duration of the still" >&2; SUDO systemctl stop mediamtx >&2; sleep 1; }}
g=$(mktemp); echo {grab_b64} | base64 -d > $g
python3 -u $g {gargs}; rc=$?
rm -f $g
if [ $main = 1 ]; then
  cur=$(v4l2-ctl -d /dev/video0 --get-ctrl=exposure,analogue_gain,gain | sed 's/: /=/' | paste -sd, -)
  SUDO systemctl start mediamtx >&2; sleep 4
  # after the stream starts the sensor may not hold the cached value, and
  # v4l2 does not write a value equal to its cache to the register: first a neighbour, then the target
  v4l2-ctl -d /dev/video0 --set-ctrl=exposure=4000,analogue_gain=2000,gain=300 >&2
  v4l2-ctl -d /dev/video0 --set-ctrl=$cur >&2
fi
exit $rc
"""
# The script is shipped in base64 (like Invoke-Cam in snap.ps1): quotes do not survive
# Windows -> ssh -> bash escaping, and bash starts only after the
# script has been decoded completely.
b64 = base64.b64encode(board.encode()).decode()
remote = f"f=$(mktemp) && echo {b64} | base64 -d > $f && bash $f; rc=$?; rm -f $f; exit $rc"
cmd = [ssh_exe, "-i", a.key, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
if a.transport == "ssh":
    cmd += ["-c", a.cipher]
cmd += [f"radxa@{a.host}", remote]
p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)   # stderr passes straight through to the caller
log = []


if a.transport == "tcp":
    line = b""
    while not line.startswith(b"READY "):
        line = p.stdout.readline()
        if not line:
            p.wait()
            sys.exit(f"board did not start the still (ssh, code {p.returncode})")
        if not line.startswith(b"READY "):
            log.append(line.decode("utf-8", "replace").rstrip())
    stage("ssh+start")
    sock = socket.create_connection((a.host, int(line.split()[1])), timeout=15)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    recv_into = sock.recv_into
else:
    raw = p.stdout
    recv_into = raw.readinto

buf = bytearray(SZ)
mv = memoryview(buf)
fr = np.frombuffer(buf, np.uint8)


def frame():
    got = 0
    while got < SZ:
        try:
            k = recv_into(mv[got:])
        except OSError as e:
            print(f"frame receive interrupted: {e}", file=sys.stderr)
            return False
        if not k:
            return False
        got += k
    return True


# Accumulation as frames arrive: the uint32 sum is exact and matches the
# former float64 sum; for max — uint8, lossless.
if a.reduce == "max":
    ay = np.empty((H, W), np.uint8)
    auv = np.empty((H // 2, W), np.uint8)
    fy, fuv = fr[:W * H].reshape(H, W), fr[W * H:].reshape(H // 2, W)
else:
    acc = np.zeros(SZ, np.uint32)
t = None
for k in range(a.n):
    if not frame():
        if a.transport == "tcp":
            sock.close()
        p.wait()
        sys.exit(f"board delivered {k} of {a.n} frames (ssh, code {p.returncode})")
    if t is None:
        t = time.perf_counter()   # as before: from the first frame to the last
        stage("warm-up+1st frame" if a.transport == "tcp" else "ssh+start+warm-up+1st frame")
    if a.reduce == "max":
        # by maximum luma; colour from the same frame where the pixel is brighter
        # (colour of a 2x2 block follows its top-left pixel, as it was on the board)
        if k == 0:
            ay[:] = fy
            auv[:] = fuv
        else:
            upd = fy > ay
            np.copyto(ay, fy, where=upd)
            np.copyto(auv, fuv, where=np.repeat(upd[::2, ::2], 2, 1))
    else:
        np.add(acc, fr, out=acc)
# frames per second between the first and last frame (undefined for n=1)
fps = (a.n - 1) / (time.perf_counter() - t) if a.n > 1 else 0.0
stage("frames")
if a.transport == "tcp":
    sock.close()
rest = p.stdout.read().decode("utf-8", "replace")
p.wait()
log += rest.splitlines()
if p.returncode != 0:
    sys.exit("board finished the still with an error (code %d)\n%s" % (p.returncode, "\n".join(log)))
stage("ssh tail")

if a.reduce == "max":
    Y = ay.astype(np.float64)
    uv = auv.astype(np.float64).reshape(H // 2, W // 2, 2)
else:
    m = acc / a.n
    Y = m[:W * H].reshape(H, W)
    uv = m[W * H:].reshape(H // 2, W // 2, 2)


def png(path, img, level):
    h, w = img.shape[:2]
    color = 0 if img.ndim == 2 else 2
    depth = 16 if img.dtype == np.uint16 else 8
    rows = (img.astype(">u2") if depth == 16 else img).reshape(h, -1).view(np.uint8)
    raw = np.zeros((h, rows.shape[1] + 1), np.uint8)   # filter byte 0 at the start of each row
    raw[:, 1:] = rows
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), level)) + chunk(b"IEND", b""))
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def png_async(path, img):
    th = threading.Thread(target=png, args=(path, img, a.png_level))   # zlib releases the GIL
    th.start()
    return th


d = os.path.dirname(a.out)
if d:
    os.makedirs(d, exist_ok=True)
y = np.clip(Y - a.black, 0, None)
jobs = [png_async(a.out + "-y16.png", np.clip(y * 256, 0, 65535).astype(np.uint16))]

# BT.601 limited range -> RGB, then white balance and percentile stretch
U = np.repeat(np.repeat(uv[..., 0], 2, 0), 2, 1) - 128
V = np.repeat(np.repeat(uv[..., 1], 2, 0), 2, 1) - 128
yl = (Y - 16) * 1.164
rgb = np.stack([yl + 1.596 * V, yl - 0.392 * U - 0.813 * V, yl + 2.017 * U], -1)
rgb *= np.array([float(v) for v in a.wb.split(",")])
top = np.percentile(rgb, a.stretch)
jobs.append(png_async(a.out + ".png", np.clip(rgb * 255 / max(top, 1), 0, 255).astype(np.uint8)))

py = np.percentile(Y, [1, 50, 99, 99.9])
info = (f"{W}x{H} n={a.n} exposure={a.exposure} again={a.again} dgain={a.dgain} "
        f"wb={a.wb} black={a.black} capture_fps={fps:.1f}\n"
        f"Y: p1={py[0]:.1f} p50={py[1]:.1f} "
        f"p99={py[2]:.1f} p99.9={py[3]:.1f} "
        f"clip={(Y >= 250).mean() * 100:.3f}%\n")
for j in jobs:
    j.join()
if not all(os.path.exists(a.out + s) for s in ("-y16.png", ".png")):
    sys.exit("PNGs were not written")
with open(a.out + ".txt", "w", encoding="utf-8", newline="\n") as f:
    f.write(info)
stage("processing+PNG")
print(info, end="")
print(f"Live stream stays at exposure={a.exposure} analogue_gain={a.again} gain={a.dgain} "
      f"(shared sensor controls, not restored after the still)")
prev = T0
parts = []
for name, ts in stages:
    parts.append(f"{name} {ts - prev:.2f}")
    prev = ts
print(f"time: {', '.join(parts)}; total {prev - T0:.2f} s ({a.transport}). "
      + " ".join(l for l in log if l.startswith("GRAB")))
