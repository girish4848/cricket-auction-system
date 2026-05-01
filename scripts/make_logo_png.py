"""One-shot raster logo for headers (crisp on Windows without Cairo). Run: py scripts/make_logo_png.py"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "static", "images", "gladiators-logo.png")

W, H = 640, 160


def _font(size: int, prefer: tuple[str, ...]) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    candidates = []
    for name in prefer:
        candidates.extend(
            [
                os.path.join(windir, "Fonts", name),
                "/usr/share/fonts/truetype/dejavu/" + name.lower(),
            ]
        )
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)

    img = Image.new("RGBA", (W, H), (7, 5, 6, 255))
    draw = ImageDraw.Draw(img)

    # Soft gold frame (matches site chrome)
    pad = 6
    draw.rounded_rectangle(
        [pad, pad, W - pad, H - pad],
        radius=14,
        outline=(212, 175, 55, 140),
        width=2,
    )

    # Simplified helmet lockup (white), scaled from SVG viewBox 320x80 → 640x160
    sx = 2.0
    ox, oy = 16 * sx, 10 * sx

    draw.pieslice(
        [ox + 40, oy + 24, ox + 136, oy + 112],
        start=200,
        end=340,
        fill=(255, 255, 255, 255),
    )
    draw.ellipse([ox + 56, oy + 56, ox + 88, oy + 82], fill=(7, 5, 6, 255))
    draw.rectangle([ox + 72, oy + 96, ox + 132, oy + 112], fill=(255, 255, 255, 242))

    font_title = _font(52, ("georgia.ttf", "Georgia.ttf", "DejaVuSerif.ttf"))
    font_sub = _font(22, ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"))

    draw.text((224, 58), "GLADIATORS", font=font_title, fill=(255, 255, 255, 255))
    draw.text((224, 118), "PREMIER LEAGUE", font=font_sub, fill=(255, 255, 255, 225))

    img.save(OUT, "PNG", optimize=True)
    print("Wrote", OUT)


if __name__ == "__main__":
    sys.exit(main() or 0)
