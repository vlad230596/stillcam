#!/usr/bin/env python3
"""Test of cam-still 2 series mode without a camera: service + fake_v4l2ctl.py (Linux/WSL).

    python3 tests/test_cam_still_series.py
    CAMSTILL_TEST_SIZE=1024x768 python3 tests/test_cam_still_series.py

Starts the service on a free port with 64x48 frames at 10 fps and checks:
  * fresh=1: the first request starts one v4l2-ctl stream, the following ones take frames from it
    (exactly one stream in the fake's log), frames are numbered consecutively, the skip/settle gap
    is visible in the header; response time is the frames, not a restart;
  * frames are fresh: the first delivered frame number is greater than the last frame number before the request + skip;
  * a one-off request without fresh stops the series and works as before (its own stream with --stream-count);
  * /series/stop and idle stop (keep);
  * 400 on invalid parameters.
Exit code 0 — everything passed.
"""
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# CAMSTILL_TEST_SIZE=1024x768 — frames larger than socket buffers: a slow client really stalls sending (frame pinning)
W, H = (int(v) for v in os.environ.get("CAMSTILL_TEST_SIZE", "64x48").split("x"))
FPS = 10.0
FB = W * H * 3 // 2


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def still(base, query):
    t0 = time.monotonic()
    status, body = get(base + "/still?" + query)
    dt = time.monotonic() - t0
    assert body[:4] == b"STL1", body[:40]
    (n,) = struct.unpack("<I", body[4:8])
    head = json.loads(body[8:8 + n])
    frames = body[8 + n:]
    assert len(frames) == head["frames"] * head["frame_bytes"], (len(frames), head)
    return head, frames, dt


def main():
    tmp = tempfile.mkdtemp(prefix="camstill-")
    env = dict(os.environ, FAKE_V4L2_STATE=os.path.join(tmp, "state.json"), FAKE_V4L2_LOG=os.path.join(tmp, "calls.log"),
               FAKE_V4L2_FPS=str(FPS))
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    svc = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "node", "cam-still"), "--port", str(port), "--bind", "127.0.0.1",
                            "--width", str(W), "--height", str(H), "--no-system-checks",
                            "--v4l2-ctl", os.path.join(HERE, "fake_v4l2ctl.py")],
                           env=env, stderr=open(os.path.join(tmp, "service.log"), "w"))
    try:
        for _ in range(50):
            try:
                get(base + "/status", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        st = json.loads(get(base + "/status")[1])
        assert st["service"] == "cam-still 3" and not st["series"]["running"], st

        # series: the first request takes longer (start + reassert + 4 frames), then skip + frames
        h1, _, t1 = still(base, "frames=2&fresh=1&keep=5")
        h2, _, t2 = still(base, "frames=2&fresh=1&keep=5")
        h3, _, t3 = still(base, "frames=1&fresh=1&keep=5&settle=4")
        print("fresh: %.2f / %.2f / %.2f s, first %s / %s / %s" % (t1, t2, t3, h1["series"]["first"], h2["series"]["first"], h3["series"]["first"]))
        assert h1["service"] == "cam-still 3" and h1["series"]["first"] >= 6, h1
        # freshness: the second request came two frames after the first — its frames are later by skip
        assert h2["series"]["first"] >= h1["series"]["first"] + 2 + 2, (h1, h2)
        assert h3["series"]["first"] >= h2["series"]["first"] + 2 + 2 + 4, (h2, h3)
        period = 1 / FPS
        assert t2 < 2 + (2 + 2) * period + 0.5, t2          # skip 2 + 2 frames, no restart
        calls = open(env["FAKE_V4L2_LOG"]).read().splitlines()
        streams = [c for c in calls if "--stream-mmap" in c]
        assert len(streams) == 1 and "--stream-count" not in streams[0], streams
        assert json.loads(get(base + "/status")[1])["series"]["running"]

        # one-off still: the series stops, its own stream with --stream-count
        h4, _, t4 = still(base, "frames=2")
        assert "series" not in h4 and h4["skip"] == 5, h4
        assert not json.loads(get(base + "/status")[1])["series"]["running"]
        streams = [c for c in open(env["FAKE_V4L2_LOG"]).read().splitlines() if "--stream-mmap" in c]
        assert len(streams) == 2 and "--stream-count=2" in streams[1], streams
        print("one-shot after series: %.2f s" % t4)

        # series again, explicit stop
        still(base, "frames=1&fresh=1&keep=5")
        assert json.loads(get(base + "/status")[1])["series"]["running"]
        get(base + "/series/stop")
        assert not json.loads(get(base + "/status")[1])["series"]["running"]

        # idle stop: keep=1 — after ~1–1.5 s there is no stream
        still(base, "frames=1&fresh=1&keep=1")
        time.sleep(2.2)
        assert not json.loads(get(base + "/status")[1])["series"]["running"], "series did not stop when idle"

        # slow client: 6 frames (ring of 8), small receive buffer and pauses longer than a frame — sending stalls,
        # the stream goes on, but the frames being delivered are pinned and not overwritten
        c = socket.socket()
        c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 << 10)
        c.connect(("127.0.0.1", port))
        c.sendall(b"GET /still?frames=6&fresh=1&keep=5 HTTP/1.1\r\nHost: x\r\n\r\n")
        body = bytearray()
        t0 = time.monotonic()
        while True:
            chunk = c.recv(16 << 10)
            if not chunk:
                break
            body += chunk
            time.sleep(0.002)
        c.close()
        slow = time.monotonic() - t0
        body = body[body.index(b"\r\n\r\n") + 4:]
        (n,) = struct.unpack("<I", body[4:8])
        head = json.loads(bytes(body[8:8 + n]))
        print("slow client: %.2f s for 6 frames (%.1f frame periods), %d bytes" % (slow, slow * FPS, len(body) - 8 - n))
        assert len(body) - 8 - n == 6 * FB, (len(body) - 8 - n, 6 * FB, head)
        try:
            get(base + "/still?frames=7&fresh=1")
            raise AssertionError("frames=7 > ring-2 accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400, e.code

        # invalid parameters
        try:
            get(base + "/still?frames=1&fresh=1&skip=99")
            raise AssertionError("skip=99 accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400, e.code
        print("OK")
        return 0
    finally:
        svc.kill()
        svc.wait()
        sys.stdout.write(open(os.path.join(tmp, "service.log")).read()[-2000:])


if __name__ == "__main__":
    sys.exit(main())
