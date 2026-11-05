"""Write server.ico (green disc with a white ring) and dashboard.ico (blue tile with rising bars).

Stdlib only; run once from this folder: python make_icons.py. The .ico files are committed.
"""
import struct
from pathlib import Path

CLEAR = (0, 0, 0, 0)
WHITE = (255, 255, 255, 255)


def server(x: int, y: int, n: int) -> tuple[int, int, int, int]:
    c, r = (n - 1) / 2, n / 2 - 0.5
    d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
    if d > r:
        return CLEAR
    return WHITE if 0.45 * r < d < 0.62 * r else (34, 139, 34, 255)


def dashboard(x: int, y: int, n: int) -> tuple[int, int, int, int]:
    if not (0 < x < n - 1 and 0 < y < n - 1):
        return CLEAR
    w = max(1, n // 6)
    for i, height in enumerate((0.35, 0.6, 0.85)):  # three bars, left to right
        left = n // 6 + i * (w + max(1, n // 12))
        if left <= x < left + w and y >= n - 2 - int(height * (n - 4)):
            return WHITE
    return (30, 90, 200, 255)


def image(pixel, n: int) -> bytes:
    rows = b"".join(
        b"".join(struct.pack("BBBB", b, g, r, a) for r, g, b, a in (pixel(x, y, n) for x in range(n)))
        for y in reversed(range(n))  # BMP rows run bottom-up
    )
    mask = bytes(((n + 31) // 32 * 4) * n)
    header = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, len(rows) + len(mask), 0, 0, 0, 0)
    return header + rows + mask


def write_ico(path: Path, pixel, sizes=(16, 32, 48)) -> None:
    images = [image(pixel, n) for n in sizes]
    out, offset = struct.pack("<HHH", 0, 1, len(images)), 6 + 16 * len(images)
    for n, data in zip(sizes, images):
        out += struct.pack("<BBBBHHII", n, n, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(out + b"".join(images))


if __name__ == "__main__":
    here = Path(__file__).parent
    write_ico(here / "server.ico", server)
    write_ico(here / "dashboard.ico", dashboard)
