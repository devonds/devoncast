"""Generates a plain 1400x1400 cover image, the minimum podcast apps expect."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 1400


def make_cover(path: Path, title: str) -> Path:
    img = Image.new("RGB", (SIZE, SIZE), "#1d3557")
    draw = ImageDraw.Draw(img)
    for i, color in enumerate(["#457b9d", "#a8dadc", "#f1faee"]):
        r = 380 - i * 110
        draw.ellipse((SIZE // 2 - r, 470 - r, SIZE // 2 + r, 470 + r), outline=color, width=26)
    font = _font(150)
    w = draw.textlength(title, font=font)
    draw.text(((SIZE - w) / 2, 1020), title, font=font, fill="#f1faee")
    img.save(path, "PNG")
    return path


def _font(size: int):
    for name in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)
