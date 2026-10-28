#!/usr/bin/env python3
"""Writes an .img.xz straight to a Windows physical disk and reads it back.

Called by flash-sd.ps1 from an elevated shell, after the disk has been cleared
(no volumes, so Windows lets raw writes through). Standard library only:
the image is decompressed on the fly, nothing is unpacked to disk.

    python flash_sd.py IMAGE.img.xz DISK_NUMBER [--sha256 HEX]
"""
import argparse
import hashlib
import lzma
import os
import sys
import time

SECTOR = 512
BLOCK = 4 << 20  # raw device I/O must be sector-aligned in size and offset


def log(msg):
    print(msg, flush=True)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(BLOCK), b""):
            h.update(chunk)
    return h.hexdigest()


def write_image(image, dev):
    """Decompresses into the device; returns (bytes written, sha256 of the image)."""
    h = hashlib.sha256()
    dec = lzma.LZMADecompressor()
    pending = bytearray()
    total = 0
    t0 = time.monotonic()
    next_report = 256 << 20
    with open(image, "rb") as src:
        while True:
            raw = src.read(1 << 20)
            if raw:
                pending += dec.decompress(raw)
            elif not dec.eof:
                raise RuntimeError("image ends before the end of the xz stream")
            while len(pending) >= BLOCK or (not raw and pending):
                part = bytes(pending[:BLOCK])
                del pending[:BLOCK]
                h.update(part)
                total += len(part)
                if len(part) % SECTOR:
                    part += b"\0" * (SECTOR - len(part) % SECTOR)
                os.write(dev, part)
                if total >= next_report:
                    mb_s = total / (time.monotonic() - t0) / 1e6
                    log(f"  written {total >> 20} MiB, {mb_s:.1f} MB/s")
                    next_report += 256 << 20
            if not raw:
                break
    return total, h.hexdigest()


def read_back(dev, total):
    h = hashlib.sha256()
    os.lseek(dev, 0, os.SEEK_SET)
    left = total
    while left > 0:
        n = min(BLOCK, -(-left // SECTOR) * SECTOR)
        data = os.read(dev, n)
        if not data:
            raise RuntimeError(f"device ended {left} bytes early")
        h.update(data[:left])
        left -= min(len(data), left)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("disk", type=int)
    ap.add_argument("--sha256", help="expected sha256 of the .img.xz file")
    a = ap.parse_args()

    if a.sha256:
        log(f"checking sha256 of {a.image}")
        got = file_sha256(a.image)
        if got != a.sha256.lower():
            log(f"ERROR: sha256 {got}, expected {a.sha256}")
            return 1
        log("  ok")

    path = rf"\\.\PhysicalDrive{a.disk}"
    log(f"writing to {path}")
    dev = os.open(path, os.O_RDWR | os.O_BINARY)
    try:
        t0 = time.monotonic()
        total, written_sha = write_image(a.image, dev)
        os.fsync(dev)
        log(f"  {total} bytes in {time.monotonic() - t0:.0f} s, image sha256 {written_sha}")
        log("reading back")
        back_sha = read_back(dev, total)
    finally:
        os.close(dev)
    if back_sha != written_sha:
        log(f"ERROR: read back sha256 {back_sha} differs from the image")
        return 1
    log("  read back matches the image")
    log("FLASH OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
