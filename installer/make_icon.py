"""Draw the D Studio icon with Pillow (no binary asset in the repo).

    python installer/make_icon.py --out dstudio.ico [--png dstudio.png]

A rounded dark tile with a white "D" and a red recording dot, rendered at
1024 px and downsampled for every .ico size (16 ... 256).
"""
from __future__ import annotations

import argparse
import os
import sys

ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


def _d_mask(s: int, x0: float, y0: float, x1: float, y1: float, stroke: float):
    """Mask of a letter D: (stem rect + right half-ellipse) minus the same shape inset."""
    from PIL import Image, ImageChops, ImageDraw

    def solid(ax0, ay0, ax1, ay1):
        m = Image.new("L", (s, s), 0)
        d = ImageDraw.Draw(m)
        xm = (ax0 + ax1) / 2
        d.rectangle([ax0, ay0, xm, ay1], fill=255)
        d.pieslice([xm - (ax1 - xm), ay0, ax1, ay1], start=-90, end=90, fill=255)
        return m

    outer = solid(x0, y0, x1, y1)
    inner = solid(x0 + stroke, y0 + stroke, x1 - stroke, y1 - stroke)
    return ImageChops.subtract(outer, inner)


def render(size: int = 1024):
    from PIL import Image, ImageDraw

    s = size
    # background tile: vertical gradient clipped to a rounded square
    grad = Image.new("RGBA", (1, s))
    top, bottom = (36, 52, 78), (13, 17, 23)
    for y in range(s):
        t = y / (s - 1)
        grad.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    grad = grad.resize((s, s))
    box = [s * 0.04, s * 0.04, s * 0.96, s * 0.96]
    tile_mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(tile_mask).rounded_rectangle(box, radius=s * 0.2, fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(grad, (0, 0), tile_mask)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(box, radius=s * 0.2, outline=(77, 163, 255, 255), width=max(2, int(s * 0.03)))

    # white "D"
    white = Image.new("RGBA", (s, s), (236, 241, 246, 255))
    img.paste(white, (0, 0), _d_mask(s, s * 0.24, s * 0.23, s * 0.74, s * 0.77, s * 0.12))

    # red recording dot (with a dark ring so it reads on the D)
    d = ImageDraw.Draw(img)
    r = s * 0.09
    cx, cy = s * 0.77, s * 0.25
    d.ellipse([cx - r * 1.4, cy - r * 1.4, cx + r * 1.4, cy + r * 1.4], fill=(13, 17, 23, 255))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 77, 77, 255))
    return img


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render the D Studio .ico")
    ap.add_argument("--out", required=True, help="output .ico path")
    ap.add_argument("--png", help="also write a 256 px PNG")
    args = ap.parse_args(argv)
    try:
        from PIL import Image
    except ImportError:
        print("make_icon: Pillow is required (pip install pillow)", file=sys.stderr)
        return 2
    big = render(1024)
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    big.resize((256, 256), Image.LANCZOS).save(out, format="ICO", sizes=ICO_SIZES)
    if args.png:
        big.resize((256, 256), Image.LANCZOS).save(os.path.abspath(args.png), format="PNG")
    print(f"make_icon: wrote {out}" + (f" and {args.png}" if args.png else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
