# stillcam — notes for agents

Network camera for machine vision; overview and layout in [README.md](README.md). Existing client applications
depend on the HTTP protocol of `cam-still` (:8081) and `cam-panel` (:8080) and store the camera's IP address in their
own configuration. Keep the HTTP protocol of both services backward compatible.

## Rules

* Everything under `node/`, `tests/` and `image/**/*.sh` runs on Linux: **LF only** (`.gitattributes` enforces it;
  `provision.sh` refuses a bundle with CR). `.ps1` files keep their UTF-8 BOM (Windows PowerShell 5.1).
* Board-side code is Python 3 standard library and bash: the camera may sit on a network without internet.
  Host-side Python dependencies — uv (`pyproject.toml`, `uv.lock`).
* Never commit passwords. The stock image password is `radxa`; `push.ps1` asks for a new one and sends it inside the
  remote script, nothing is stored.
* Read the pitfalls in [docs/radxa-zero3.md](docs/radxa-zero3.md) before touching the capture or encode pipeline:
  `GST_MPP_NO_RGA=1` stays, the sensor mode is never pinned through `media-ctl`, the 3A service is not toggled while
  streaming. `image/radxa-zero3/reference/cam-probe-modes.obsolete` pins the sensor mode — never install it.
* Each camera is unique: hostname from the SoC serial, its own fallback address `169.254.176.N` (asked by `push.ps1`).
  Do not reintroduce shared names or addresses; do not clone a configured card to make a second camera.

## Checks

```
python3 tests/test_cam_still_series.py        # Linux / WSL
python3 tests/test_cam_still_focus.py
bash -n node/cam-pipeline image/radxa-zero3/provision.sh
```

## Status

* 2026-10-28: second camera's SD card written from the official image and verified by read-back;
  `push.ps1` / `provision.sh` have **not run on a board yet** — checklist in [docs/image.md](docs/image.md).
* `provision.sh` installs `python3-numpy` (2026-10-28): `node/stack.py` (`host/snap.ps1 -Stack`) needs it on the board;
  the first camera had it, the first version of the script forgot it.
