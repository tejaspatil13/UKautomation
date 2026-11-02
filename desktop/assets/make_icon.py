"""Draws the app icon (the dashboard's blue brand dot on a rounded tile) as copilot.png and copilot.ico.
Standard library only:  python desktop/assets/make_icon.py"""
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).parent
ACCENT = (0x23, 0x57, 0xC6)
TILE = (0xFF, 0xFF, 0xFF)
EDGE = (0xE1, 0xE4, 0xE9)


def pixels(size: int) -> bytes:
    s = 4  # supersampling for smooth edges
    rows = []
    for y in range(size):
        row = bytearray([0])  # PNG filter: none
        for x in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(s):
                for sx in range(s):
                    px, py = (x + (sx + 0.5) / s) / size, (y + (sy + 0.5) / s) / size
                    color, alpha = _sample(px, py)
                    for i in range(3):
                        acc[i] += color[i] * alpha
                    acc[3] += alpha
            a = acc[3] / (s * s)
            rgb = [int(acc[i] / acc[3]) if acc[3] else 0 for i in range(3)]
            row += bytes([*rgb, int(a * 255)])
        rows.append(bytes(row))
    return b"".join(rows)


def _sample(x: float, y: float):
    r = 0.22  # tile corner radius
    m = 0.04  # margin
    cx, cy = min(max(x, m + r), 1 - m - r), min(max(y, m + r), 1 - m - r)
    d_tile = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    if d_tile > r:
        return (0, 0, 0), 0.0
    if ((x - 0.5) ** 2 + (y - 0.5) ** 2) ** 0.5 <= 0.25:
        return ACCENT, 1.0
    return (EDGE if d_tile > r - 0.025 else TILE), 1.0


def png(size: int) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels(size), 9))
            + chunk(b"IEND", b""))


def ico(sizes=(16, 24, 32, 48, 64, 128, 256)) -> bytes:
    images = [png(s) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, img in zip(sizes, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    return out + b"".join(images)


if __name__ == "__main__":
    (HERE / "copilot.png").write_bytes(png(512))
    (HERE / "copilot.ico").write_bytes(ico())
    print("wrote copilot.png and copilot.ico")
