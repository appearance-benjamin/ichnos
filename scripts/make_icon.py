"""Render ichnos/assets/icon.svg to PNGs and a Windows .ico, with the Chrome or Edge used for PDFs.

Run once after changing the SVG:  python scripts/make_icon.py
Writes icon-16.png ... icon-512.png and ichnos.ico (PNG-compressed frames) next to the SVG.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ichnos.document import find_browser  # noqa: E402

ASSETS = ROOT / "ichnos" / "assets"
SIZES = [16, 24, 32, 48, 64, 128, 192, 256, 512]


def render(svg: Path, size: int, out: Path, browser: str) -> None:
    page = out.with_suffix(".html")
    page.write_text(f"<!doctype html><html><head><style>html,body{{margin:0;background:transparent;}}"
                    f"img{{display:block;width:{size}px;height:{size}px;}}</style></head><body>"
                    f"<img src='{svg.resolve().as_uri()}'></body></html>", encoding="utf-8")
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
                        "--default-background-color=00000000", f"--window-size={size},{size}",
                        f"--screenshot={out}", page.resolve().as_uri()], check=True, timeout=120, capture_output=True)
    page.unlink(missing_ok=True)


def ico(pngs: list[tuple[int, bytes]], out: Path) -> None:
    """An .ico with PNG frames (Windows Vista and later read them at any size)."""
    head = struct.pack("<HHH", 0, 1, len(pngs))
    entries, blobs, offset = b"", b"", 6 + 16 * len(pngs)
    for size, data in pngs:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    out.write_bytes(head + entries + blobs)


def main() -> None:
    browser = find_browser()
    if not browser:
        raise SystemExit("no Chrome or Edge found")
    svg = ASSETS / "icon.svg"
    frames = []
    for size in SIZES:
        png = ASSETS / f"icon-{size}.png"
        render(svg, size, png, browser)
        if size <= 256:
            frames.append((size, png.read_bytes()))
        print(png.name, png.stat().st_size, "bytes")
    ico(frames, ASSETS / "ichnos.ico")
    print("ichnos.ico", (ASSETS / "ichnos.ico").stat().st_size, "bytes")


if __name__ == "__main__":
    main()
