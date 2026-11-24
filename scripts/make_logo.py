"""LiveType logo: three sound-wave bars turning into a text cursor ("voice becomes text, live").

python scripts/make_logo.py preview <out.png>   — variants side by side, at several sizes
python scripts/make_logo.py build <variant>      — writes assets/ (SVG, PNGs, ICO)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
S = 1024  # design canvas

VARIANTS = {
    "violet": {"bg": ("#7C5CFF", "#2BB5FF"), "bars": "#FFFFFF", "caret": "#FFFFFF", "glow": True},
    "midnight": {"bg": ("#111827", "#3730A3"), "bars": "#C7D2FE", "caret": "#34D399", "glow": False},
    "mint": {"bg": ("#0FB57A", "#0EA5E9"), "bars": "#FFFFFF", "caret": "#FFFFFF", "glow": True},
    "coral": {"bg": ("#FF5F6D", "#FFC371"), "bars": "#FFFFFF", "caret": "#FFFFFF", "glow": True},
}

# geometry on the 1024 canvas
RADIUS = 232
BARS = [(214, 300), (360, 470), (506, 250)]  # (x, height) — bar width 96, centred on y=512
BAR_W = 96
CARET_X, CARET_TOP, CARET_BOT, STEM_W, SERIF_W, SERIF_H = 742, 232, 792, 70, 214, 64


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def render(variant: str, size: int) -> Image.Image:
    v = VARIANTS[variant]
    k = 4 if size < 256 else 2  # supersampling
    n = size * k
    sc = n / S
    a, b = np.array(hex_rgb(v["bg"][0]), float), np.array(hex_rgb(v["bg"][1]), float)
    yy, xx = np.mgrid[0:n, 0:n] / n
    t = np.clip((xx + yy) / 2, 0, 1)[..., None]
    rgb = (a * (1 - t) + b * t).astype(np.uint8)
    img = Image.fromarray(rgb, "RGB").convert("RGBA")
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, n - 1, n - 1), radius=int(RADIUS * sc), fill=255)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    if v["glow"]:  # soft top highlight
        hl = Image.new("L", (n, n), 0)
        ImageDraw.Draw(hl).ellipse((-0.2 * n, -0.75 * n, 1.2 * n, 0.45 * n), fill=38)
        img = Image.composite(Image.new("RGBA", (n, n), (255, 255, 255, 255)), img, hl)
        img.putalpha(mask)
        d = ImageDraw.Draw(img)
    bars = hex_rgb(v["bars"])
    for x, h in BARS:
        d.rounded_rectangle(((x) * sc, (512 - h / 2) * sc, (x + BAR_W) * sc, (512 + h / 2) * sc),
                            radius=BAR_W / 2 * sc, fill=bars)
    c = hex_rgb(v["caret"])
    d.rounded_rectangle(((CARET_X - STEM_W / 2) * sc, CARET_TOP * sc, (CARET_X + STEM_W / 2) * sc, CARET_BOT * sc),
                        radius=STEM_W / 2 * sc, fill=c)
    for y in (CARET_TOP, CARET_BOT - SERIF_H):
        d.rounded_rectangle(((CARET_X - SERIF_W / 2) * sc, y * sc, (CARET_X + SERIF_W / 2) * sc, (y + SERIF_H) * sc),
                            radius=SERIF_H / 2 * sc, fill=c)
    return img.resize((size, size), Image.LANCZOS)


def svg(variant: str) -> str:
    v = VARIANTS[variant]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}" role="img" aria-label="LiveType logo">',
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{v["bg"][0]}"/><stop offset="1" stop-color="{v["bg"][1]}"/></linearGradient></defs>',
        f'<rect width="{S}" height="{S}" rx="{RADIUS}" fill="url(#g)"/>',
    ]
    if v["glow"]:
        parts.append(f'<clipPath id="c"><rect width="{S}" height="{S}" rx="{RADIUS}"/></clipPath>'
                     f'<ellipse clip-path="url(#c)" cx="{S / 2}" cy="{-0.15 * S}" rx="{0.7 * S}" ry="{0.6 * S}" '
                     'fill="#fff" fill-opacity="0.15"/>')
    for x, h in BARS:
        parts.append(f'<rect x="{x}" y="{512 - h / 2}" width="{BAR_W}" height="{h}" rx="{BAR_W / 2}" fill="{v["bars"]}"/>')
    c = v["caret"]
    parts.append(f'<rect x="{CARET_X - STEM_W / 2}" y="{CARET_TOP}" width="{STEM_W}" height="{CARET_BOT - CARET_TOP}" '
                 f'rx="{STEM_W / 2}" fill="{c}"/>')
    for y in (CARET_TOP, CARET_BOT - SERIF_H):
        parts.append(f'<rect x="{CARET_X - SERIF_W / 2}" y="{y}" width="{SERIF_W}" height="{SERIF_H}" '
                     f'rx="{SERIF_H / 2}" fill="{c}"/>')
    parts.append("</svg>")
    return "".join(parts)


def preview(out: str) -> None:
    sizes = [256, 64, 32, 16]
    cell = 300
    sheet = Image.new("RGB", (cell * len(VARIANTS), cell * 2), "#F3F4F6")
    dark = Image.new("RGB", (cell * len(VARIANTS), cell), "#1F2937")
    sheet.paste(dark, (0, cell))
    for i, name in enumerate(VARIANTS):
        for row in range(2):
            x0, y0 = i * cell + 20, row * cell + 20
            sheet.paste(render(name, 160), (x0, y0), render(name, 160))
            x = x0 + 170
            for s in sizes[1:]:
                im = render(name, s)
                sheet.paste(im, (x, y0 + 60), im)
                x += s + 12
        ImageDraw.Draw(sheet).text((i * cell + 20, 2 * cell - 24), name, fill="#E5E7EB")
    sheet.save(out)


def build(variant: str) -> None:
    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    (out / "logo.svg").write_text(svg(variant), encoding="utf-8")
    for s in (16, 24, 32, 48, 64, 128, 256, 512, 1024):
        render(variant, s).save(out / f"logo-{s}.png")
    render(variant, 256).save(out / "livetype.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                          (128, 128), (256, 256)])
    print("assets written:", sorted(p.name for p in out.iterdir()))


if __name__ == "__main__":
    if sys.argv[1] == "preview":
        preview(sys.argv[2])
    else:
        build(sys.argv[2])
