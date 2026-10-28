# cam-still — full-frame still service (port 8081)

`cam-still` is a permanent, deliberately "dumb" service on the camera board: it returns raw NV12 frames from the ISP
main path (3280×2464) over HTTP, without ssh. Averaging, colour, black level and PNG are done on the PC by a client
(`host/still.py`, or a client application that reproduces its processing bit for bit). Camera internals and
pitfalls are described in [radxa-zero3.md](radxa-zero3.md); building the camera image — [image.md](image.md).

## The `cam-still` service (board, port 8081)

Files in [node](../node): `cam-still` (Python, stdlib only), `cam-still.service` (systemd, `User=radxa`),
`install-cam-still.sh`; in [tests](../tests): `fake_v4l2ctl.py` (a stand-in for testing without a camera).

| Request | Response |
|---|---|
| `GET /still?frames=N` | N consecutive frames, N from 1 to 128. Optional parameters: `pre=1`, `post=4`, `reassert=1` |
| `GET /still?frames=N&fresh=1` | **series mode** (cam-still 2, 2026-09-30): the main path stream is kept running between requests, N (≤ 6) frames captured after the request are returned. Parameters `skip=2` (fresh frames to discard), `settle=0` (this many more after a controls change), `keep=10` (seconds to keep the stream after the request) |
| `GET /series/stop` | stop the series stream immediately (otherwise it stops by itself after `keep` or on a one-shot request) |
| `GET /status` | JSON: `busy`, `device`, `width`, `height`, `max_frames`, `autoexposure`, `series` |

Body of `/still`. `Content-Length` is exact: if the body arrived shorter, the capture was cut off.

```
"STL1"                 4 bytes, magic
uint32 LE              length L of the JSON header
L bytes JSON           {"format":"NV12","width":3280,"height":2464,"stride":3280,
                        "frame_bytes":12122880,"frames":N,"skip":5,
                        "controls":{"exposure":…,"analogue_gain":…,"gain":…},
                        "device":"/dev/video0","service":"cam-still 1"}
N × frame_bytes        NV12: Y (height rows of stride), then UV (height/2 rows)
```

**The NV12 layout within a frame is a convention, not a header field.** The UV plane follows **immediately** after Y:
UV offset = `stride·height`, UV rows also use `stride`, and `frame_bytes` = exactly `stride·height·3/2`. There is no
separate `uv_offset` in the header: `cam-still` itself computes `frame_bytes` with this formula (`set_format`), and a
mismatch with the driver's `Size Image` is only logged as a warning. A client should accept **only** this
`frame_bytes` and reject anything else as a bad response ("not our layout"), rather than compute colour from foreign
bytes. If the driver ever starts aligning UV separately, both sides
must change: the service adds `uv_offset` to the header, the client takes it from there.

The service reads `controls` (`--get-ctrl`) before the capture. If they could not be read, the value is `null`.

Errors come as JSON `{"error": code, "message": text}`:

| HTTP code | `error` | When |
|---|---|---|
| 400 | `bad_request` | parameters out of range |
| 409 | `busy` | another still is already running (only one still at a time) |
| 409 | `autoexposure` | `rkaiq_3A` is running: switch to manual mode via the panel first |
| 503 | `main_path_busy` | the stream itself is on `/dev/video0` (mode wider than 1920) |
| 500 | `capture_failed` | `v4l2-ctl` did not accept the format, the capture did not start |

The capture works exactly as in `still_grab.py`: the same `v4l2-ctl --stream-mmap=4 --stream-skip=pre+post
--stream-count=N --stream-to=-`, and `v4l2-ctl` itself writes the frames into the socket. After `pre` frames the
service rewrites the **current** controls using the "neighbouring value, then the wanted one" scheme (the controls cache
pitfall), then discards another `post` frames. The service does not choose the exposure: it is set by the panel
(`:8080`, `POST /api/camera`). The service does not touch the codec, RGA or the sensor mode. The live stream runs on
the self path and is not interrupted.

Differences from the ssh path (intentional):

* if the stream is on the main path, `still.py` stops it for the duration of the still, while the service answers 503.
  The service never touches the stream;
* with 3A enabled the service answers 409, as `still.py` does. The client turns 3A off via the panel before asking for a
  still;
* the service listens on `0.0.0.0:8081` permanently. Previously the port was open only for the duration of the still
  and only for the address from `$SSH_CLIENT`. It only returns camera frames and changes nothing on the board except
  re-writing controls that are already set.

### Installing on the board

Done 2026-09-25, see "Deployed". System OpenSSH; sudo access — [radxa-zero3.md](radxa-zero3.md), "Access".
`<camera-ip>` below is the camera's address; on a camera built from [image.md](image.md) the service is installed by
`provision.sh`.

```
scp -i ~/.ssh/radxa_cam node/cam-still node/cam-still.service node/install-cam-still.sh radxa@<camera-ip>:/tmp/
ssh -i ~/.ssh/radxa_cam radxa@<camera-ip> 'echo "<password>" | sudo -S -p "" sh /tmp/install-cam-still.sh'
```

The script refuses to install files with CRLF (pitfall 5), checks the syntax, installs `/usr/local/bin/cam-still` and
the unit, does `enable` + `restart`, and prints the `rga` count (must stay at 5). What to check after installing:

1. `curl http://<camera-ip>:8081/status` → `busy:false`, `autoexposure:false` (in manual mode).
2. Take `/still?frames=16` while the stream is running. `ActiveEnterTimestamp` of `mediamtx` does not change, the
   stream fps on the panel does not drop beyond what the exposure dictates, `dmesg | grep -c rga` = 5, and
   `journalctl -u cam-still` has a `GRAB … fps` line.
3. Speed: expected to be the same as `still_grab.py` over TCP (~9.6 fps, limited by gigabit).
4. Comparison on live frames: capture the same scene with `snap.ps1 -Still` and with the service. The difference must
   be like that between two consecutive stills (median |Δ| y16 ~0.8 LSB).
5. Check that `stride` = 3280 and that there is no `Size Image` warning in the log: `frame_bytes` is taken as
   stride·H·3/2, as `still.py` has always computed it.

Removal: `systemctl disable --now cam-still`, then delete both files.

### cam-still 2 (2026-09-30)

Installed with the same `install-cam-still.sh` on 2026-09-30: `/usr/local/bin/cam-still` md5
`fafcd7a0…` (matches the working copy), the previous version kept next to it — `/usr/local/bin/cam-still.v1`
(`b6eb327f…`). Rollback:
`sudo install -m 755 /usr/local/bin/cam-still.v1 /usr/local/bin/cam-still && sudo systemctl restart cam-still`. After
installation `rga` = 5, `mediamtx` was not restarted, the live stream ran at 19.2–19.4 fps, also during a series. The
one-shot path did not change. What a client is expected to do on its side: series requests (skip, settle, keep),
trusting the controls (no panel call when the controls are unchanged), per-stage still timing, and optionally a tap
for raw frames for research.

### cam-still 3: focus mode (2026-10-20)

**Why.** The lens cannot be adjusted by looking at the compressed stream (`:8889`) — sharpness is not visible in it,
and a 12 MB full frame cannot be sent live. The service cuts several small squares (Y, unscaled) and a decimated
overview out of every full-resolution frame — that is kilobytes per frame.

**How to use.** The "Focus (full frame)" button on the panel (`:8080`), or directly `http://<camera-ip>:8081/focus`
— from any device on the network, handy from a phone next to the camera. The camera must be in manual exposure (as
for stills). By default three 128×128 crops — left edge, centre, right edge at mid-height; a click on the overview sets
the centre of the active crop (select with digits 1…8 or by clicking a tile), up to 8 crops of 64…512, zoom ×1…×6,
everything is remembered in the browser. Each crop shows: a sharpness number, "% of peak" with a bar (green from 98 %),
history, and the peak since the last reset (button or `R`). Turn the lens until all crops approach their peaks;
numbers of **different** crops must not be compared.

Sharpness is the gradient energy from central differences divided by (mean brightness − 16)²: it is almost
independent of exposure. EMA smoothing 0.35. Overexposure (≥ 250) in more than 1 % of a crop — a red mark: the metric
is wrong there.

**Service.** `GET /focus/stream?rects=x,y,w,h;…&ov=8` — an endless stream of `FCS1` packets (format — in the header
of `cam-still`: `"FCS1"`, uint32 LE length L, L bytes JSON `{"seq","dropped","fps","controls","width","height",
"rects":[[x,y,w,h]...],"overview":[w,h,step]?,"service"}`, then the crops row by row in order, then the overview) on
top of the main path series stream; each time the **latest** frame is taken and intermediate ones are discarded, so a
slow client does not accumulate lag. On the board (2026-10-20, exposure 2738): **19.3 fps — every sensor frame, 0
dropped**; 3 crops of 128² + overview 1/8 — 3.4 MB/s, first packet after 0.35 s; the service's `python3` ~65 % of one
core, `v4l2-ctl` ~35 %; the live stream meanwhile at 19.3 fps, `publishing`. The estimate "Python relay ~90 MB/s" from
the `cam-still` header was not confirmed for reading into the ring: 19.3 × 12 MB ≈ 233 MB/s. While focus mode is
running the service holds the lock: stills get 409 `busy` with the client's address; a new `/focus/stream` takes the
stream over from the old one (the page reconnects every time the crops change); a closed tab is noticed on the next
frame, a client that vanished without closing — by the 5 s send timeout. The live stream is not affected (same as
with a series).

Tested without a camera: `python3 tests/test_cam_still_focus.py` (WSL, fake `v4l2-ctl`) — byte-for-byte comparison of
the crops and the overview with the frame, takeover, 409 during focus, still afterwards, 400. The page — in a browser
on a 3280×2464 test image via the fake at 5 fps.

**Installation.** A camera built from [image.md](image.md) gets cam-still 3 and the panel with the "Focus" button from
`provision.sh`. To update an existing camera: `install-cam-still.sh` (cam-still) and the command from
[radxa-zero3.md](radxa-zero3.md#how-it-is-built), "How it is built" (cam-panel, for the button); before updating, save
`/usr/local/bin/cam-still` as `cam-still.v2` for rollback. Check afterwards: actual fps and board CPU load, `rga` = 5,
that `mediamtx` was not restarted.

### Deployed (2026-09-25)

Installed with `install-cam-still.sh` unmodified, the script worked on the first try: `/usr/local/bin/cam-still` (md5
`b6eb327f…`, matches the working copy), `/etc/systemd/system/cam-still.service` (`eda8d99a…`), `enable` + `start`.
Nothing else on the board was changed; no reboot was needed.

State before installation: `mediamtx` active since 2026-09-23 14:31:37 UTC, stream 1920×1088 on `/dev/video1`, 3A
off, controls 4095 / 1024 / 256, 12.9 fps (limited by exposure 4095), `rga` = 5, strict oops pattern = 0, 50 GB free.

| Check | Result |
|---|---|
| `/status` | `busy:false`, `autoexposure:false`, 3280×2464, `max_frames` 128; `frames=0` → 400 |
| client application (one-off console), `frames=4` | 1.4–1.7 s until a ready still (capture + processing), 1.5–1.9 s including PNG writing. On the board: 1st frame 0.23 s, warm-up 0.72 s, frames 0.38 s |
| `frames=16`, service + client application | 2.9–3.1 s until a ready still, 3.2–3.3 s with PNG; on the board 9.3 fps (frames 1.62 s). `snap.ps1 -Still` of the same — 4.6–5.1 s (ssh + start 0.67–1.22 s) |
| `frames=48` | frames 5.03 s, 9.4 fps |
| header | `stride` 3280, `frame_bytes` 12122880, no `Size Image` warning in the log |
| `radxa` permissions on `/dev/video0` under systemd | work (group `video`) |
| live stream during and after stills | `ActiveEnterTimestamp` and PIDs of `mediamtx`/`gst-launch` unchanged, panel `publishing` all the time, 12.9–13.0 fps, HLS responds; `rga` = 5, oops = 0 |
| comparison with `snap.ps1 -Still`, 16 frames, same scene | service and ssh path alternately, 2 stills each. Mean y16 72.80 / 72.81 (service) versus 72.84 / 72.89 (ssh); median \|Δ\| y16 = 48 (0.19 LSB Y), p99 176 — **the same** for service–service, ssh–ssh and service–ssh pairs, mean offset ≤ 0.09 (y16) |
| parallel request during a 48-frame still | 409 `busy`, the first still completed in full |
| `systemctl restart cam-still` | came up in < 1 s, `/status` and a 4-frame still work immediately, stream unaffected |
| the client's panel calls against the live panel | get status and set sensor controls work |

Exposure: the client set 4095 / 2816 / 256 by default (3A was already off, the stream was not interrupted). After
the checks `analogue_gain` was returned to 1024 via the panel — controls as before installation (4095 / 1024 / 256),
3A off.

Deviations and caveats:

* the scene during the check was dark (night, the light in the enclosure off, Y ≈ 16 — black level), so the
  comparison was essentially on noise and offset: it would have caught gross errors (frame shift, swapped planes, a
  different black level), but not subtle differences in bright areas. Repeat with light if needed;
* 3A was not enabled: the 409 `autoexposure` behaviour and the client turning 3A off (via the panel;
  interrupts the stream for ~5 s) were not tested on the live board;
* the board was not rebooted — that the service comes up at boot follows only from `enabled`
  (`WantedBy=multi-user.target`).

### Testing without a camera (WSL)

```
R=$(pwd)   # repository root
FAKE_V4L2_FRAMES=<an NV12 file of 64×48 frames, e.g. a mean of 16 frames saved by a client> \
  python3 $R/node/cam-still --port 18081 --width 64 --height 48 \
  --no-system-checks --v4l2-ctl $R/tests/fake_v4l2ctl.py
# in another window, on Windows (address 127.0.0.1: on localhost .NET HttpClient waits ~20 s for IPv6):
set CAM_STILL_TEST_URL=http://127.0.0.1:18081/
# then run a client's live tests against it
```

A client's live tests read `CAM_STILL_TEST_URL`; without it they should check nothing, so a normal test run does not
need a camera or the fake.
