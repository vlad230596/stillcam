#!/usr/bin/env python3
"""Test of cam-still 3 focus mode without a camera: service + fake_v4l2ctl.py (Linux/WSL).

    python3 tests/test_cam_still_focus.py

Starts the service on a free port with 640x480 frames at 20 fps and checks:
  * GET /focus returns the page;
  * /focus/stream: FCS1 packets arrive in sequence, frame numbers increase, crops and overview match the
    fake's frame byte for byte, a crop at the frame edge is clamped inside;
  * while focus is running, a still gets 409 busy mentioning focus, /status shows the client;
  * a new /focus/stream takes the stream over from the old one (the old one gets the end of the body);
  * after focus is closed, stills work again;
  * 400 on invalid parameters.
Exit code 0 — everything passed.
"""
import http.client
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
W, H, FPS = 640, 480, 20.0
FB = W * H * 3 // 2
# fake frames without FAKE_V4L2_FRAMES: byte i of frame k = ((i % 256) * 7 + k) & 0xFF, k = 0..2 cyclically
PATTERNS = [bytes((x * 7 + k) & 0xFF for x in range(256)) * (FB // 256) for k in range(3)]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def http_error(url):
    try:
        get(url)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    raise AssertionError("no error for " + url)


def open_stream(port, query):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", "/focus/stream?" + query)
    r = c.getresponse()
    assert r.status == 200, (r.status, r.read()[:300])
    return c, r


def read_exact(r, n):
    data = r.read(n)
    if len(data) != n:
        raise EOFError("short read: %d of %d" % (len(data), n))
    return data


def packet(r):
    pre = read_exact(r, 8)
    assert pre[:4] == b"FCS1", pre
    (n,) = struct.unpack("<I", pre[4:])
    head = json.loads(read_exact(r, n))
    need = sum(q[2] * q[3] for q in head["rects"])
    if head.get("overview"):
        need += head["overview"][0] * head["overview"][1]
    return head, read_exact(r, need)


def check_content(head, data):
    """Crops and overview come from one fake frame (one of the three patterns)."""
    for frame in PATTERNS:
        o, ok = 0, True
        for x, y, w, h in head["rects"]:
            want = b"".join(frame[(y + r) * W + x:(y + r) * W + x + w] for r in range(h))
            ok &= data[o:o + w * h] == want
            o += w * h
        if head.get("overview"):
            ow, oh, step = head["overview"]
            want = b"".join(frame[r * W:r * W + W:step] for r in range(0, H, step))
            ok &= len(want) == ow * oh and data[o:o + ow * oh] == want
        if ok:
            return
    raise AssertionError("packet #%d does not match any fake frame" % head["seq"])


def main():
    tmp = tempfile.mkdtemp(prefix="camfocus-")
    env = dict(os.environ, FAKE_V4L2_STATE=os.path.join(tmp, "state.json"), FAKE_V4L2_LOG=os.path.join(tmp, "calls.log"),
               FAKE_V4L2_FPS=str(FPS))
    env.pop("FAKE_V4L2_FRAMES", None)
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
        assert st["service"] == "cam-still 3" and not st["focus"]["active"], st
        status, page = get(base + "/focus")
        assert status == 200 and b"/focus/stream" in page, page[:200]

        # stream: the second crop at the bottom-right corner is clamped inside the frame
        c1, r1 = open_stream(port, "rects=10,20,32,16;600,470,64,64&ov=8")
        seqs = []
        t0 = time.monotonic()
        for _ in range(6):
            head, data = packet(r1)
            assert head["rects"] == [[10, 20, 32, 16], [576, 416, 64, 64]], head
            assert head["overview"] == [80, 60, 8] and head["width"] == W and head["service"] == "cam-still 3", head
            check_content(head, data)
            seqs.append(head["seq"])
        dt = time.monotonic() - t0
        assert seqs == sorted(set(seqs)), seqs
        print("focus: 6 packets in %.2f s, frames %s, fps %s" % (dt, seqs, head["fps"]))

        # while focus is running, stills are busy and /status shows the client
        code, err = http_error(base + "/still?frames=1&fresh=1")
        assert code == 409 and err["error"] == "busy" and "focus" in err["message"], (code, err)
        st = json.loads(get(base + "/status")[1])
        assert st["focus"]["active"] and st["series"]["running"], st

        # takeover: the second client takes the stream, the first gets the end of the body
        c2, r2 = open_stream(port, "rects=0,0,16,16&ov=0")
        head, data = packet(r2)
        assert head["rects"] == [[0, 0, 16, 16]] and "overview" not in head, head
        check_content(head, data)
        try:
            for _ in range(10):
                packet(r1)
            raise AssertionError("the old focus client was not cut off")
        except (EOFError, http.client.IncompleteRead, ConnectionError):
            pass
        r1.close()
        c1.close()
        print("takeover: ok")

        # page closed — stills work again (the service notices the disconnect on the next frame)
        r2.close()      # the response holds the socket: without it the connection stays open
        c2.close()
        deadline = time.monotonic() + 5
        while True:
            try:
                status, body = get(base + "/still?frames=1&fresh=1&keep=2")
                break
            except urllib.error.HTTPError as e:
                if e.code != 409 or time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        assert status == 200 and body[:4] == b"STL1"
        assert not json.loads(get(base + "/status")[1])["focus"]["active"]
        print("still after focus: ok")

        # invalid parameters
        for q in ("rects=0,0,8,8", "rects=0,0,600,16", "rects=0,0,16,16&ov=3", "rects=a,b,c,d", "ov=0",
                  "rects=" + ";".join(["0,0,16,16"] * 9)):
            code, err = http_error(base + "/focus/stream?" + q)
            assert code == 400 and err["error"] == "bad_request", (q, code, err)
        print("OK")
        return 0
    finally:
        svc.kill()
        svc.wait()
        sys.stdout.write(open(os.path.join(tmp, "service.log")).read()[-2000:])


if __name__ == "__main__":
    sys.exit(main())
