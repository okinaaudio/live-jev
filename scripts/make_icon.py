#!/usr/bin/env python3.13
"""Draw the 1024 px Live Jev app icon as a PNG using only the standard library."""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

SIZE = 1024
BACKGROUND = (250, 248, 243)
RING = (184, 69, 43)
CORE = (31, 29, 26)


def _pixel(x: int, y: int) -> tuple[int, int, int, int]:
    cx = cy = SIZE / 2
    dx, dy = x + 0.5 - cx, y + 0.5 - cy
    r = math.hypot(dx, dy)
    corner = SIZE * 0.22
    inside_rounded = _in_rounded_square(x + 0.5, y + 0.5, corner)
    if not inside_rounded:
        return (0, 0, 0, 0)
    if r < SIZE * 0.12:
        return (*CORE, 255)
    if SIZE * 0.30 < r < SIZE * 0.36:
        return (*RING, 255)
    if SIZE * 0.41 < r < SIZE * 0.44 and abs(math.atan2(dy, dx)) < math.pi * 0.75:
        return (*RING, 255)
    return (*BACKGROUND, 255)


def _in_rounded_square(px: float, py: float, radius: float) -> bool:
    inset = SIZE * 0.06
    left, top, right, bottom = inset, inset, SIZE - inset, SIZE - inset
    if px < left or px > right or py < top or py > bottom:
        return False
    qx = max(left + radius - px, px - (right - radius), 0.0)
    qy = max(top + radius - py, py - (bottom - radius), 0.0)
    return math.hypot(qx, qy) <= radius


def _chunk(tag: bytes, payload: bytes) -> bytes:
    body = tag + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def write_png(path: Path) -> None:
    rows = bytearray()
    for y in range(SIZE):
        rows.append(0)
        for x in range(SIZE):
            rows.extend(_pixel(x, y))
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + _chunk(b"IEND", b"")
    path.write_bytes(data)


def main() -> int:
    if len(sys.argv) != 2:
        print("使い方: make_icon.py <出力PNG>", file=sys.stderr)
        return 2
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    write_png(out)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
