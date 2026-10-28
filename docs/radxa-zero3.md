# Camera on Radxa ZERO 3E: how it is built, how to use it, what to avoid

A remote network camera: a Radxa ZERO 3E board (RK3566) with a
Radxa Camera 8M 219 module (Sony IMX219 sensor). It serves H.264 over RTSP/WebRTC/HLS and has
a service web panel. It was brought up on 2026-09-17…18 and works. (Some measured values below come from that first
installation: a camera looking down at a flat object on a table inside an enclosure.)

**Building a camera from scratch** (since 2026-10-27): see [image.md](image.md): the stock image + `image/radxa-zero3`;
files that used to live only on the board are now in this repository. The rest of this document describes that first camera.

**Addresses are examples.** `<camera-ip>` below is the camera's address; the host name `radxa-cam` is that of the first camera. Each camera now gets its own host name, `radxa-cam-<last 6 hex digits of the SoC serial>` —
see [image.md](image.md).

This document is written for someone who will **use** the camera, and its
"Pitfalls" section for someone who will **extend** it. The second section matters more: almost all
obvious paths here lead into a wall, and the wall hits hard — up to a hang of
the whole board with no SSH access.

## Access

| | |
|---|---|
| Host | `radxa-cam-<serial tail>`, `<camera-ip>` (DHCP; the MAC is derived from the SoC serial, so the lease is stable). The scripts `host/snap.ps1`, `host/still.py` and client scripts take the address from `RADXA_HOST`; without it they stop with an error |
| Network name | `radxa-cam.local` — mDNS works, **use it** if the address changes |
| Fallback address | `169.254.176.50/16` — permanent, set in the NetworkManager profile. Needed for a direct cable to a PC without DHCP |
| SSH | `ssh -i ~/.ssh/radxa_cam radxa@<camera-ip>`, key login |
| Key | `%USERPROFILE%\.ssh\radxa_cam` (ed25519, no passphrase) |
| `radxa` password | `<password>` — needed for `sudo` (passwordless sudo is not configured). Not stored in the repository: ask the camera owner. The scripts `host/snap.ps1` and `host/still.py` take it from the environment variable `RADXA_SUDO_PASS` |
| OS | Debian 12 bookworm, kernel `6.1.84-10-rk2410-nocsf`, Radxa image `rsdk-b1` of 2025-07-15 |

Scripts call `sudo` like this (password on stdin; otherwise `sudo` demands a terminal):

```bash
SUDO() { echo "<password>" | sudo -S -p "" "$@"; }
```

`host/snap.ps1` and `host/still.py` take the password from the environment variable `RADXA_SUDO_PASS`; without it they
stop with an error where `sudo` is needed. To set it on the workstation:

```powershell
$env:RADXA_SUDO_PASS = '<password>'                                              # for the current session
[Environment]::SetEnvironmentVariable('RADXA_SUDO_PASS', '<password>', 'User')   # permanently, for the user
```

## Usage

| What | Where to go |
|---|---|
| Service panel | `http://<camera-ip>:8080/` |
| View in a browser (WebRTC, minimal latency) | `http://<camera-ip>:8889/cam` |
| HLS (compatibility, a few seconds of latency) | `http://<camera-ip>:8888/cam` |
| RTSP for players, OBS, NVR | `rtsp://<camera-ip>:8554/cam` |

**The stream intentionally does not start at boot.** The board always comes up in
a reachable state; streaming is switched on with a panel button or with a command:

```bash
ssh -i ~/.ssh/radxa_cam radxa@<camera-ip> 'echo "<password>" | sudo -S -p "" systemctl start mediamtx'
```

Why — see "Pitfalls", the item about RGA. In short: if the camera pipeline does
hang the kernel after all, the next boot is guaranteed to be alive.

The panel can: show status (temperature, **actual** FPS, clock frequencies,
fan, load, memory), start/stop/restart the stream,
adjust camera controls, toggle auto exposure and change the stream mode.

## Grabbing a frame from a script

[host/snap.ps1](../host/snap.ps1) fetches a frame to the workstation. ffmpeg
is not needed: the frame is pulled from the local RTSP **on the board itself** via
GStreamer with the software decoder `avdec_h264` (the hardware `mpph264dec` must not be
touched — it calls RGA, see pitfalls), then goes over scp. The 9th decoded frame is taken
(`-SkipFrames 8`: the first frames after connecting to RTSP are corrupt), and the stream is torn down at once; `-Seconds` (12) is only
an upper bound. Before 2026-10-12 the stream was pulled for the full 12 s; `-SkipFrames 0` gives the old behaviour. The new
variant has not yet been checked on the real setup.

```
pwsh host/snap.ps1 -Out shots/rect.jpg
pwsh host/snap.ps1 -Stack 120 -Out shots/light.jpg
pwsh host/snap.ps1 -Exposure 400 -ManualExposure
pwsh host/snap.ps1 -AutoExposure
```

`-Stack N` combines N frames by maximum (lighten) with
[node/stack.py](../node/stack.py), which reads raw RGB from
gst-launch. This is needed for a fast-moving light spot: when a spot traces a contour faster
than the camera takes a frame (19 fps, 52 ms), a single frame shows only a piece of
the contour. 120 frames (~6 s) collect the whole contour.

The script starts `mediamtx` by itself if the stream is not running.

## Measurement still (geometry, fine detail)

For measuring geometry and fine detail on an object, a frame from RTSP **is not good enough**:
H.264 at 6 Mbit/s eats low-contrast fine detail (thin lines in dark areas, sharp edges), and the
1920×1088 stream is also cropped vertically. Instead:

```
pwsh host/snap.ps1 -Still -Out shots/still
pwsh host/snap.ps1 -Still -Frames 32 -Out shots/still32
```

The still takes NV12 **directly from the ISP main path** (`/dev/video0`) at full frame
3280×2464 (the codec and RGA are not involved; the `rga` counter in `dmesg` stays at 5) and
averages N frames (`-StillArgs "--reduce max"` combines by maximum, for
the trace of a moving light spot). It requires manual exposure (3A off).

**Scheme (since 2026-09-23): the board only serves raw frames, the workstation does the computing.**

```
snap.ps1 -Still ─→ still.py (PC, system Python with numpy)
                     │ ssh: script + still_grab.py in base64, one connection per still
                     ▼
                   still_grab.py (board, stdlib only)
                     │ listens on a random TCP port, prints "READY port",
                     │ accepts ONE connection from the address in $SSH_CLIENT, closes the port
                     │ v4l2-ctl --stream-mmap --stream-skip=5 --stream-to=-  (stdout = socket)
                     │ after the 1st frame: controls "neighbouring value, then the wanted one"
                     ▼
                   N × 12.1 MB of raw NV12 over TCP ─→ PC: uint32 sum / uint8 max
                                                    as frames arrive, colour, 2 PNGs in threads
```

* [host/still.py](../host/still.py) — **on the workstation**: ssh,
  receiving, processing, PNG. The on-board bash snippet (3A check, stopping the stream if
  it sits on the main path) lives in it too.
* [node/still_grab.py](../node/still_grab.py) — **on the board**, in a temporary file for
  the duration of the still; nothing is installed. Frames are written to the socket by
  `v4l2-ctl` itself; Python does not pass them through, numpy is not needed on the board.
* Processing is numerically the same as it was on the board: on the same raw frames
  the old and new code give **bit-identical** y16 and colour PNG (both for the
  mean and for `--reduce max`). Stills taken by the old and new path back to back
  differ exactly as two stills by the old path: |difference| of y16 has a median of 0.8 LSB
  (mean of 16), 2 LSB (max of 16).
* `--transport ssh` is a fallback if the TCP port is blocked: frames go over
  ssh stdout with the cipher `aes128-gcm@openssh.com`, ~0.8 s slower on 16 frames (6.3 fps vs 9.6).
* Warm-up: 1 frame before writing controls and 4 after (it used to be 3 + 8). Measured
  by switching `analogue_gain` 1400 → 2816: when frames are taken without delay,
  the first frame after the write is intermediate and the second is already new; with a full
  v4l2 queue (4 buffers) the new one is the fifth. 4 covers both cases.
* PNGs are written with zlib level 1: at 8 MP this is 0.3 + 0.5 s versus 2.1 + 1.2 s at
  level 6, with files of the same size (10.9 and 16.1 MB). The file format is unchanged.

Timing (2026-09-23, full call `powershell -File snap.ps1 -Still`, the way client
scripts call it):

| Stage | Before (all on the board) | Now |
|---|---|---|
| ssh, script upload, start | ~0.4 s (+ numpy on the board 0.8 s) | 0.65 s, occasionally 1.2 (first connection after a pause) |
| capture start + warm-up | 1.6 s (3 + 8 frames, gst-launch) | 0.75 s (1 + 4, v4l2-ctl) |
| 16 frames | 2.5 s (6.5 fps, bound by numpy float64); `--reduce max` 6.6 s (2.4 fps) | 1.6 s (9.6 fps, bound by gigabit, 116 MB/s) |
| processing + PNG | 19 s (numpy + zlib level 6 on A55) | 1.1–1.3 s (PC) |
| scp of 3 files, 27 MB | 1.5 s (3 connections) | — |
| **total `-Frames 16`** | **27–30 s** | **4.6–5.3 s** |
| **total `--reduce max`** | **26–28 s** | **4.6–4.7 s** |

`-Frames 4` — 4.1 s, `-Frames 24 --reduce max` — 5.4 s, `-Frames 32` — ~6 s.
Frames beyond the warm-up cost ~0.1 s each (network). Board CPU per still is up to
25 % of four cores (background with the stream ~10 %); previously one core was busy with
numpy for ~20 s.

Board → PC link throughput, 194 MB from `/dev/shm`, 1 Gbit/s link on both
sides; `nc`/`iperf3` are not on the board (we chose not to install them):

| Transport | MB/s | Board CPU (of 400 %, with ~45 % stream background) |
|---|---|---|
| raw TCP (python `sendfile`) | **116** | ~80 % |
| `ssh cat`, `aes128-gcm@openssh.com` | 74 | ~190 % |
| `ssh cat`, `aes128-ctr` | 54–60 | ~185 % |
| `ssh cat`, `chacha20-poly1305` (Windows OpenSSH **default**) | 57–63 | ~180 % |
| `scp` (default) | 42–45 | ~190 % |

The A55 has hardware AES (`Features: aes pmull`), so GCM is faster than
chacha20 — but it is still bound by the board CPU. Pumping frames through Python on the board
(pipe → socket) gave 91 MB/s, so `v4l2-ctl` writes frames to the socket itself.
`gst-launch … ! fdsink` without network delivers only 8.6 fps versus 12.9 for `v4l2-ctl`.

What else could be squeezed out and why it was not done:

* **A permanent service on the board** instead of ssh per still would save
  ~0.5 s of 0.65 (ssh handshake 0.35 s + bash/python start). The price is
  a permanently open port and one more service on the board. Not adopted.
* **Summing on the board** (uint16, numpy): 25 ms/frame, the sensor (12.9 fps) would not
  be slowed down, and a single 24 MB buffer would go over the network. Gain ~0.15 s on 16 frames and
  ~0.5 s on 32, at the cost of numpy on the board (+0.5 s import) and two implementations of
  `--reduce max`. Not worth it.
* The bottleneck now is gigabit: 16 frames ≈ 194 MB ≈ 1.6 s.

**Consequence for `--reduce max`:** 16 frames are now taken in 1.6 s instead of
6.6 s — the trace of a moving spot is collected in a quarter of the time. For a spot circling
fast (for example 500 mm/s on a 5 mm radius — ~16 revolutions per second) this does not
matter; slow motion needs more frames.

**The stream is not interrupted meanwhile** (since 2026-09-23): it runs on the second ISP output,
the self path (`/dev/video1`), and both outputs work in parallel from the same sensor
frame — see "How it is built". `ActiveEnterTimestamp` of `mediamtx` does not change
during a still, the stream's `gst-launch` stays alive, `rga` stays at 5. The exception is a mode wider than
1920 (3280×2464): the self path cannot handle it, the stream runs on the main path, and then
`snap.ps1` stops the stream for the duration of the still, as before.

**Exposure and gain of the still remain in the live stream — intentionally.** Sensor
controls are shared by both paths, and the user wants to see in the stream with which
parameters the still was taken. `still.py` and `snap.ps1` report this with the line "Live stream
left with the still's parameters: …". Consequence: after a still with `exposure=4095`
the stream runs at ~12.9 fps until exposure is set back by hand (panel or
`v4l2-ctl -d /dev/video0 --set-ctrl=exposure=2741`).

The camera is reached only through the system Windows OpenSSH. The Python for processing is the
workstation's system Python with numpy (`-Python` of `snap.ps1`, default:
`python` from PATH; a project venv without numpy will not do). The last output line of
`still.py` is the time breakdown by stage. Output:

| File | Purpose |
|---|---|
| `still-y16.png` | luminance, 16 bit, black level subtracted, **no stretching** — for measurements |
| `still.png` | colour, 8 bit, white balance from the table + stretch to p99.5 — for viewing |
| `still.txt` | capture parameters and luminance percentiles |

Tuned on the first installation on 2026-09-23, with the lighting inside its enclosure as is:

| Parameter | Value | Why |
|---|---|---|
| Resolution | 3280×2464 | ~1.7 times more pixels per mm on the object than in the stream |
| `exposure` | **4095** (ceiling) | ~77 ms; above ~2500 lines the sensor lengthens the frame itself — irrelevant for a still |
| `analogue_gain` | **2816** (×11, ceiling) | little light; analogue is the only gain that does not add noise |
| `gain` (digital) | **256** (×1) | gives nothing in the 16-bit average, only clips highlights |
| Frames | 16 (32 for the finest detail) | noise falls as √N: frame 2.2 LSB → mean ~0.55 LSB |
| White balance | R×1.68, G×1, B×1.25 | measured on a neutral grey surface without 3A |
| Black level | 16 | NV12 from the ISP is limited range; the dark object in that scene gives Y≈24 |

With this the table is not overexposed (Y p99.9 ≈ 200, clipping 0.007 %), and low-contrast detail
on the dark object stays visible — you only need to stretch the range
(Y 20…80 works well for the dark areas).

For the live stream keep `exposure=2741`, `analogue_gain=2816`: 4095 drops
the stream to ~12.9 fps, 2741 holds 19.3 (on both main path and self path — measured
2026-09-23). `snap.ps1 -Still` does **not** restore the values that were set before the still —
see above.

For the **trace of a bright moving spot**, a different mode was tuned the day before (in poor light):
`exposure=1970`, `gain=700`: less saturation, a more accurate centre line
(checked against the known path of the spot). The circular traces
taken on that day were shot in measurement-still mode (4095 / 256) —
possibly why their radius came out 2 % larger than specified.

**Pitfall: controls after the stream starts.** Right after the pipeline starts, the sensor
may hold a value other than the one in the control cache, and v4l2 does not write to the
register a value equal to the cached one. Observed: `analogue_gain=2816`
in `--get-ctrl`, but the picture as at 256 — the signal above black ~10 times weaker. The cure is to write a
neighbouring value, then the wanted one (`still_grab.py` does exactly that, after the first frame). This is probably also
part of the reason for "manual mode is darker than automatic" in the section below.

## Camera controls

Applied instantly, no stream restart needed (exception: the
auto exposure checkbox, see below). The panel calls `v4l2-ctl` on `/dev/video0`.
These are controls of the sensor itself: they are equally visible and writable via `/dev/video0`,
`/dev/video1` and `/dev/v4l-subdev3` (`m00_b_imx219`), and act on both ISP
outputs — the stream on the self path obeys writes to `/dev/video0` (verified). The same
by hand:

```bash
v4l2-ctl -d /dev/video0 --set-ctrl=exposure=900
```

| Control | Type | Range | Default | Note |
|---|---|---|---|---|
| `exposure` | int | 0…4095 | 1575 | in frame lines |
| `gain` | int | 256…43663 | 256 | digital gain |
| `analogue_gain` | int | 256…2816 | 512 | analogue gain |
| `horizontal_flip` | bool | 0/1 | 0 | |
| `vertical_flip` | bool | 0/1 | 0 | |
| `test_pattern` | menu | 0…13 | 0 | 0 — off, the rest are the sensor's built-in patterns |
| `vertical_blanking` | int | 36…36 | 36 | **read-only in practice**: `min=max` |
| `horizontal_blanking` | int | 164…164 | 164 | same |
| `pixel_rate` | int64 | — | 180810000 | read-only |
| `link_frequency` | menu | — | 456 MHz | read-only |

**Exposure and gain stick only with auto exposure off.** They are
controlled by the `rkaiq_3A.service` service; while it runs, a value set by hand
is overwritten back within a fraction of a second.

**The 3A service may be touched only with the pipeline stopped.** Toggled while running,
it takes the colour away with it — details in pitfalls, item 3. The correct order:

```bash
systemctl stop mediamtx
systemctl stop rkaiq_3A      # manual mode; for auto exposure — restart rkaiq_3A
systemctl start mediamtx
```

The panel checkbox does exactly this: it breaks the stream for ~5 seconds and brings it back
up — but only if 3A really has to be switched; a request for the state
that is already set does not touch the stream. The restart resets the sensor to defaults (`exposure=158`,
`analogue_gain=256`), so when switching to manual mode the panel itself
restores the values that auto exposure had been holding.

Verified: with 3A stopped, a value holds indefinitely.

Manual mode is noticeably darker than automatic at the same `exposure`/`gain`: without
rkaiq its tone mapping on the ISP side does not work; only the sensor gains remain.
It is compensated with digital `gain` (the ceiling 43663 is another ×3.8 over the typical 11486).
White balance also freezes in manual mode; the picture turns to a cold tint.

## Stream modes

Changed on the panel; they require a stream restart (a few seconds of interruption).
The list comes from `/usr/local/etc/cam-modes.conf`.

| Resolution | Frame rate | Note |
|---|---|---|
| 3280×2464 | ~19 fps | full frame, 8 MP; main path only — the stream is interrupted during `-Still` |
| **1920×1088** | ~19 fps | current working mode, "almost Full HD" |
| 1600×1200 | ~19 fps | 2 MP 4:3 |
| 1280×720 | ~19 fps | HD |
| 640×480 | ~19 fps | VGA |

Plus bitrate (default 6 Mbit/s) and key frame interval (30).
Actual output at 1920×1088: **19.3–19.5 fps, 5.5–5.6 Mbit/s**, verified by
pulling RTSP to the workstation. On the self path at `exposure=2741` — the same
19.27 fps, including while the main path is taking 3280×2464 in parallel.

"Apply and restart" with the same parameters as the running stream does
**not** restart the stream (since 2026-09-23): it restarts only when the config changed or
the stream is stopped. Likewise the auto exposure checkbox does nothing if 3A
is already in the wanted state — scripts request manual mode before every still.

**1920×1080 is absent from the list intentionally** — see pitfalls.

## How it is built

```
IMX219 → MIPI CSI → ISP (rkisp) ─┬→ self path /dev/video1 (rkisp_selfpath) → GStreamer:
                                  │    v4l2src → mpph264enc (hardware H.264) → h264parse → tee
                                  │                                   ├→ rtspclientsink → MediaMTX → network
                                  │                                   └→ fpsdisplaysink → /run/cam-fps
                                  └→ main path /dev/video0 (rkisp_mainpath) → still_grab.py → TCP → still.py on the PC, on demand
```

**Two ISP outputs.** `rkisp_mainpath` (`/dev/video0`) delivers up to 3280×2464,
`rkisp_selfpath` (`/dev/video1`) — NV12 and others, stepwise 32×32…1920×2464, step 8.
Both scale the same sensor frame and work simultaneously: the stream
lives on the self path, and the main path is free for a measurement still at full
frame. Verified 2026-09-23: while the self path streams 1920×1088, the main path
delivers 3280×2464 (`v4l2-ctl` takes 12.9 fps at exposure 4095, 9.6 go
to the network — bound by gigabit), the stream fps
does not drop beyond what the exposure dictates, `rga` = 5, no errors in the
`mediamtx` log. `DEVICE` in `cam-stream.conf` sets the stream node;
`cam-pipeline` moves the stream to the main path by itself if the width is above 1920.
The sensor mode is still not pinned (pitfalls, item 2).

| File | Purpose |
|---|---|
| `/usr/local/bin/cam-pipeline` | builds and starts the GStreamer pipeline, reads parameters from the config. **All critical decisions live here — read its comments before editing**. Source: [node/cam-pipeline](../node/cam-pipeline) |
| `/usr/local/etc/cam-stream.conf` | stream parameters: `WIDTH`, `HEIGHT`, `BITRATE`, `GOP`, `DEVICE` (`/dev/video1`). Sample: [node/cam-stream.conf](../node/cam-stream.conf) |
| `/usr/local/etc/cam-modes.conf` | list of resolutions for the panel, checked for safety |
| `/usr/local/bin/cam-panel` | web panel, Python on the standard library, no external dependencies (the board may be on a network without internet). Source in this repository: [node/cam-panel](../node/cam-panel) |
| `/usr/local/bin/cam-probe-modes` | queries the sensor and regenerates the list of its modes. **Do not run**: it pins the sensor mode through `media-ctl` (pitfall 2) and overwrites the carefully checked `cam-modes.conf`; not part of the new image ([image.md](image.md)) |
| `/usr/local/bin/cam-blackbox` | once a second writes link state, error counters, temperature, clock frequencies, memory to `/var/log/cam-blackbox.log` |
| `/usr/local/etc/mediamtx.yml` | MediaMTX v1.21.0 config, `runOnInit` points to `cam-pipeline` |
| `/config/config.txt` | on the card's FAT partition. Contains `disable_service mediamtx` — the "stream does not start by itself" policy |

Services: `mediamtx` (disabled — manual), `cam-panel` (enabled), `cam-blackbox`
(enabled), `rkaiq_3A` (enabled, auto exposure), `cam-still` (enabled since
2026-09-25, :8081, stills for client applications — [cam-still.md](cam-still.md)).

This repository holds `cam-panel`, `cam-pipeline` and the sample `cam-stream.conf`
([node](../node)); the files installed on the board must match them
(`md5sum` on the board against `Get-FileHash -Algorithm MD5` locally). On the first camera the remaining board
files existed **only on the board** (the image now carries them — [image.md](image.md)). Deploying:

```bash
scp -i ~/.ssh/radxa_cam node/cam-panel node/cam-pipeline radxa@<camera-ip>:/tmp/
ssh -i ~/.ssh/radxa_cam radxa@<camera-ip> 'echo "<password>" | sudo -S -p "" sh -c "install -m 755 /tmp/cam-panel /usr/local/bin/cam-panel && install -m 755 /tmp/cam-pipeline /usr/local/bin/cam-pipeline && systemctl restart cam-panel"'
```

Restarting `cam-panel` does not touch the stream. A new `cam-pipeline` takes effect at
the next start of `mediamtx` — it does not change the running pipeline.

The journal is written to the card (`/var/log/journal`) and survives hangs — this is
the only thing that made it possible to find the cause of the failures. Do not disable it.

The hardware watchdog is on: `/etc/systemd/system.conf.d/10-watchdog.conf`,
`RuntimeWatchdogSec=30s`. On a kernel hang the board reboots by itself in about
44 seconds.

---

# Pitfalls

This is where time is really lost. Ordered by the cost of the mistake.

## 1. The RGA block corrupts kernel memory. Do not touch it

**The main trap.** The RGA driver (Rockchip 2D accelerator) of this kernel
has a bug: `rga_request_put_current_mm()` releases a reference to the process memory
structure that it never took. The counter goes negative, it triggers
`WARNING at kernel/fork.c:790 __mmdrop`, then kernel memory gets corrupted, and
the board dies — sometimes after twenty seconds, sometimes after ten minutes.

The insidious part is that **a random innocent task ends up in the trace**:

```
__mmdrop  ←  rga_request_put_current_mm  ←  rga_request_submit  ←  rga_ioctl_blit
__mmdrop  ←  finish_task_switch                    (Comm: mpph264enc0:src)
```

The first trace is the scene of the crime; the others are already consequences that surfaced
later on an ordinary task switch. You cannot find the cause from them.

**How it is protected now:** `cam-pipeline` exports `GST_MPP_NO_RGA=1`. The
`rockchipmpp` plugin calls RGA only when a frame needs to be repacked, so
all offered resolutions are multiples of 16 on both sides — no repacking is needed.
The RGA call counter per boot stays at 5 (these are the driver's initialisation
lines), and the board works.

**What this means when extending:**

* Any resolution where the width or height is not divisible by 16 will make the plugin
  call RGA. That is exactly why there is no 1920×1080: 1080 / 16 = 67.5. Instead,
  1920×1088.
* If a size does require repacking, the pipeline honestly fails to negotiate
  (`not-negotiated`) — loud and harmless. This is correct behaviour, not "breakage".
* Check like this: `dmesg | grep -c rga` before and after. If it grew, you called RGA.
* A side note: `mem=3838M` on the kernel command line cuts off 256 MB of memory above
  the 4 GB boundary, because the RGA MMU is 32-bit. This removes **one** of the
  triggers, but not the bug itself. No need to remove it; it does no harm.

## 2. Do not pin the sensor mode through media-ctl

It looks logical: the IMX219 has 1920×1080@30 and 1640×1232@30 modes, so you could
get 30 frames instead of 19. **It does not work and makes things worse.**

If you set the sensor mode by hand (`media-ctl --set-v4l2 ...[fmt:SRGGB10_1X10/1920x1080]`),
the ISP starts losing frames, the log fills with `rkisp-vir0: MIPI drop frame`, and
capture drops:

| Pinned mode | Capture 1280×720 | Capture 1600×1200 |
|---|---|---|
| 1640×1232 @30 | 14.1 fps | 2.4 fps |
| 1920×1080 @30 | 14.2 fps | — |
| **not pinned (full frame @21)** | **19.4 fps** | 19.4 fps |

The state persists until reboot — so one failed experiment
spoils the board until a reboot. In the worst case degradation down to 0.5 fps was observed.

The driver itself keeps the sensor in the full-frame mode 3280×2464@21 and scales
through the ISP. This is faster than any other option. `cam-pipeline` does not touch the sensor
at all, and that is deliberate.

## 3. The 3A service must not be switched on the fly — it takes the colour away

Two different stories, and the second one is more expensive.

**The first, harmless:** auto exposure overrides manual values. You set
an exposure, it blinks and goes back. `rkaiq_3A` is to blame; the cure is to stop
it. This is how the system works, not a failure.

**The second:** `rkaiq_3A_server` attaches to the ISP **only on the stream start
event**, which it receives from `/dev/video9`. If you touch the service while the pipeline
is already running, both sides break:

| What was done | What we got |
|---|---|
| `stop` while running | the picture instantly turns **black and white**: ISP parameters stay at defaults, colour modules are not updated |
| `start` while running | the log shows `DBG: /dev/media0: wait stream start event...` and then silence — the event has already passed, 3A hangs forever. Colour does not come back, auto exposure does not work |
| `start` while running + stream restart afterwards | an endless `ANALYZER:E:stats disorder, latest:974, new:761` in the log — the statistics counter is out of sync, the analyser is dead |

Hence the complaint "I turned auto exposure off — the stream went black and white, I turned it
back on — it stayed that way, only a restart helps".

**The correct order — only with `mediamtx` stopped:**

```bash
systemctl stop mediamtx
systemctl restart rkaiq_3A     # or stop, if manual mode is needed
systemctl start mediamtx
```

Exactly `restart`, not `start`: the daemon hanging since last time must first be
removed. The panel (`set_autoexposure()` in `cam-panel`) has done this by itself since 2026-09-22;
the price is a ~5 second stream interruption, as with a mode change. Since 2026-09-23
it first checks the current state of `rkaiq_3A` and does nothing if it matches:
before that, every `snap.ps1 -ManualExposure` broke the stream for nothing.

Check it in the log: `journalctl -t rkaiq --since -3min | grep -c "stats disorder"`
must give **0**, and the end must contain `ACCM:` lines — that is the colour correction
matrix at work.

## 4. If the board does not respond — edit the card, do not look for a way to reach it

A proven recovery procedure when SSH is unavailable:

1. Power off, remove the card, insert it into a card reader on the workstation.
2. **`wsl --mount` does not work with removable media** — this is a documented
   limitation; on a card reader you will always get `ERROR_FILE_NOT_FOUND`. Do not waste
   time on it.
3. Instead: read the FAT partition `/config` sector by sector (16 MB at offset
   16 777 216), edit it through a loop device in WSL, write it back.
   A ready-made script, `fixconfig.ps1`, was in the session's working folder; the logic is simple
   and can be reproduced in ten minutes.
4. `/config/config.txt` accepts `rsetup` commands, executed at **every**
   boot before anything else: `disable_service`, `enable_service`, `add_user`,
   `update_password`, `update_hostname`, `connect_wi-fi`, `resize_root`. This is
   enough to regain control.

One more detail: Windows may mark the card as `Offline` by policy, and then
even sector-level reading fails. The cure is `Set-Disk -Number N -IsOffline $false`
from PowerShell with administrator rights.

## 5. Editing files from Windows: watch the line endings

Python on Windows writes CRLF. The result:

* a shell script stops running with a mysterious `no such file or directory` —
  the kernel looks for the interpreter `/bin/bash\r`;
* multi-line search-and-replace silently fails to find the anchor, and the patch is applied
  partially, leaving a file that looks working but is broken.

Write files in binary mode with `\n` and check that every replacement actually
took effect.

## 6. Diagnostics: what to trust

* `dmesg | grep -i Oops` catches the word **ram**oops** and shows nonexistent
  crashes. A strict pattern: `__mmdrop|Bad rss-counter|Internal error|Oops:`.
* Windows `ping` counts a "Destination host unreachable" reply from **its own**
  address as a successful packet and shows "0% loss". Read the reply text, not
  the final statistics.
* A hung board keeps its link up — the switch port state says nothing about its
  health.
* Do not scan the whole subnet. Find the board by `<hostname>.local` or by its
  known address.

## 7. Frame rate ceiling

The question "can we have 60 or 120?" is closed: **no, and the limit is the sensor itself.**

| Link | Headroom |
|---|---|
| IMX219 + MIPI 2 lanes × 456 MHz | ~182 Mpix/s — **used up to the limit** |
| Sensor driver | frame blanking `min=max` in all modes — the frame rate is not adjustable at all |
| ISP (297 MHz) | ~140 fps at 1080p — not a constraint |
| RK3566 encoder | rated for 1080p60 — not a constraint |

Arithmetic from the board's own data: line length 3444 clocks, at 1080p there are 1766 lines
per frame, total 6 082 104 clocks per frame, 182 463 120 / 6 082 104 = exactly 30.0.
Even with blanking cut to the physical minimum, 1080p would give ~47 fps.

What can be done:

* **~47 fps at 1080p** — only by patching the sensor driver and rebuilding the kernel.
  The BSP has already proven fragile; assess the risk soberly.
* **60 and 120 fps** — only with a different camera module. 120 needs a sensor such as
  the OV9281 with a global shutter.
* The IMX219 has no intermediate resolutions: the driver reduces any request to
  the nearest of its four modes (3280×2464, 1920×1080, 1640×1232, 640×480).

## 8. Power

Two sources at once — PoE HAT and USB-C — **must not** be connected. With both,
the board heated up to 65–71 °C at idle instead of the usual 44 °C. Power it from one source.

PoE on the ZERO 3E works only with the HAT installed, and the copper standoffs and screws
are part of the power circuit — without them power over the cable will not flow.

The HAT fan is enabled by the `radxa-zero3-poe-hat` overlay (it controls only
the fan and has nothing to do with power delivery). Effect: under load
**85 °C with constant throttling → 57 °C without it**. Without the fan the board
hits the second thermal trip point and throttles the CPU clock.

---

## What is not finished

* **The `mediamtx` unit is not hardened.** The plan was to add `KillMode=control-group`,
  `TimeoutStopSec=15` and a restart rate limit, so that unkillable processes do not
  accumulate when the pipeline hangs. The script that was doing this
  died together with the board, and the change was not applied. Currently it has `Restart=always`,
  `RestartSec=2`. Not critical with the present configuration, but worth
  revisiting when extending.
* **Stream autostart is off.** If you need "power on — streaming right away",
  remove `disable_service mediamtx` from `/config/config.txt` and enable the service.
  In exchange you lose the guarantee that after a failure the board comes up in a known
  working state.
* **Manual white balance and other fine ISP tuning** were not touched. Everything
  rkaiq can do is available only through its own interfaces, not through v4l2.
* **Long-term operation** was tested up to 15 minutes of continuous streaming.
  There was no multi-day test.
* **The "wider than 1920 → main path" branch in `cam-pipeline` was tested only with a dry
  run** (with a substituted `gst-launch`). The normal 1920×1088 mode on the new
  version was tested with a live start on 2026-09-23 14:31: the log shows
  `cam-pipeline: 1920x1088 on /dev/video1`, 19.27 fps at exposure 2741.
* **Warnings `invalid FU-A packet` / `RTP packet is missing`** in the
  `mediamtx` log appeared on 2026-09-23 around 13:37 and show up near stills —
  of both the old and the new scheme. Looks like board or network load during
  a still; not investigated. If the picture jerks during a still, this is it.
* **The field of view of the stream on the self path was not compared** with the former one on the main path:
  cropping when scaling 3280×2464 → 1920×1088 may differ. This does not affect the
  measurement still — it goes through the main path at full frame.
