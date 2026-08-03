#!/usr/bin/env python3
"""Render the app SVG into a multi-size Windows .ico.

Run from the MSYS2 UCRT64 Python so GdkPixbuf's SVG loader (librsvg) is available.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402
from PIL import Image  # noqa: E402

SIZES = [16, 24, 32, 48, 64, 128, 256]


def render(svg_path: Path, size: int) -> Image.Image:
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(svg_path), size, size, True)
    ok, data = pixbuf.save_to_bufferv("png", [], [])
    if not ok:
        raise RuntimeError(f"failed to render {svg_path} at {size}px")
    return Image.open(io.BytesIO(data)).convert("RGBA")


def main() -> int:
    svg_path = Path(sys.argv[1])
    ico_path = Path(sys.argv[2])
    ico_path.parent.mkdir(parents=True, exist_ok=True)

    images = [render(svg_path, size) for size in SIZES]
    images[-1].save(ico_path, format="ICO", sizes=[(img.width, img.height) for img in images])
    print(f"wrote {ico_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
