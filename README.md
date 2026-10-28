# stillcam

Network camera for machine vision. A small Linux SBC with a camera sensor runs three services:

| Port | Service | What it does |
|---|---|---|
| 8554 | RTSP via [MediaMTX](https://github.com/bluenviron/mediamtx) (`rtsp://<camera>:8554/cam`) | live H.264 stream for watching, hardware-encoded |
| 8889 / 8888 | WebRTC / HLS via MediaMTX (`http://<camera>:8889/cam`) | the same stream in a browser |
| 8081 | `cam-still` | **full-resolution raw frames** (NV12, straight from the ISP, no codec) over HTTP; one-shot and series modes; a **live focus page** (`/focus`) with full-resolution crops and a sharpness metric |
| 8080 | `cam-panel` | web panel: telemetry, sensor controls applied immediately (exposure, gain, flips), stream parameters, start/stop |

The stream and the stills come off two independent ISP outputs of the same sensor frame, so a measurement
still never interrupts the live stream. Exposure and gain are never chosen by the still service: the client sets them
through the panel and gets exactly what it asked for. Averaging, colour, black level and PNG are done on the host.

The camera is built **from the stock vendor image** by a script (`image/`): no golden image to clone, so every unit gets its own
host keys, machine-id and hostname (`radxa-cam-<last 6 hex digits of the SoC serial>`), and several cameras can share one network.

Current hardware: **Radxa ZERO 3E** (RK3566) + **IMX219** (8 MP), official image rsdk-b1 (Debian 12). The services
are plain Python 3 (standard library only) and bash; the board-specific part is the image recipe and a few kernel and ISP
pitfalls described in [docs/radxa-zero3.md](docs/radxa-zero3.md).

## Security

stillcam is meant for a trusted LAN. Nothing is authenticated: the panel (runs as root, so that it can start and stop
the stream), the still service and the stream are open to anyone who can reach the camera, and MediaMTX keeps its
default listeners (RTSP, WebRTC, HLS, and also RTMP 1935, SRT 8890, RTP/RTCP 8000/8001). Put the camera on an isolated
network or behind a firewall; do not expose it to the internet. ssh: key login is installed by `push.ps1`, password
login stays enabled as a fallback with the password you set during provisioning.

## Layout

```
node/                runs on the camera
  cam-still            still + focus service (:8081)          docs/cam-still.md
  cam-panel            web panel (:8080)
  cam-pipeline         capture -> encode -> RTSP, started by MediaMTX
  cam-stream.conf      stream parameters
  still_grab.py        board half of host/still.py: raw frames over ssh-started TCP (uploaded per shot)
  stack.py             max-stack of N stream frames (host/snap.ps1 -Stack)
  install-cam-still.sh update cam-still alone on a running camera
host/                runs on the PC
  still.py             measurement still: N raw frames -> mean / max -> PNG
  snap.ps1             one frame from the camera to the PC (JPEG from the stream, or -Still)
tests/               cam-still without a camera: fake v4l2-ctl + service tests (Linux / WSL)
image/radxa-zero3/   building a camera from the official Radxa image   docs/image.md
  flash-sd.ps1, flash_sd.py   write the image to an SD card and verify it by reading back (Windows)
  push.ps1                    set up a freshly flashed board over the network (Windows)
  provision.sh                the set-up itself, runs on the board as root
  rootfs/                     files installed as-is (units, journald / logrotate limits, watchdog)
  reference/                  snapshot of the first hand-built camera (package list, file hashes)
docs/
```

## Quick start: a new camera

1. Download the official image and write it to an SD card (Windows, asks for elevation):
   ```
   powershell -File image/radxa-zero3/flash-sd.ps1 -DiskNumber <N> -Image radxa-zero3_bookworm_kde_b1.output_512.img.xz
   ```
2. Boot the board on a LAN with DHCP and internet access, wait a few minutes for the first boot.
3. Set it up:
   ```
   powershell -File image/radxa-zero3/push.ps1 -CamHost radxa-zero3.local -FallbackOctet 51 -Reboot
   ```
   It asks for the fallback-address octet (unique per camera), the current password (`radxa` on the stock image) and a new one.

Details, the per-camera checklist and open questions are in [docs/image.md](docs/image.md).

## Using it

```
python host/still.py --host <camera> -n 16 --out shots/still      # mean of 16 raw frames -> PNG
powershell -File host/snap.ps1 -Out shots/view.jpg                 # one JPEG from the stream
```
Open `http://<camera>:8080/` for the panel and `http://<camera>:8081/focus` to set focus.
The HTTP protocol of `cam-still` (headers, series mode, focus stream packets) is in [docs/cam-still.md](docs/cam-still.md).

Host Python dependencies are managed with [uv](https://docs.astral.sh/uv/): `uv sync`, then `uv run host/still.py ...`.

## Testing without a camera

```
python3 tests/test_cam_still_series.py
python3 tests/test_cam_still_focus.py
```
Linux or WSL: the tests start the real `cam-still` against `tests/fake_v4l2ctl.py`.

## Documentation

* [docs/radxa-zero3.md](docs/radxa-zero3.md) — the camera on Radxa ZERO 3E: access, controls, stream modes, how it is built, pitfalls
* [docs/cam-still.md](docs/cam-still.md) — still service: protocol, series mode, focus mode, deployment, testing
* [docs/image.md](docs/image.md) — building a camera from the official image, checklist

## License

MIT, see [LICENSE](LICENSE).
