#!/usr/bin/env python3
"""Builds the page's icons from the app icon (the repo's branding/icon.svg and branding/icon-master.png):

    python3 site/tools/make-icons.py path/to/branding

Writes to site/assets/:
- favicon.svg, a copy of icon.svg
- favicon-16.png, favicon-32.png, favicon-48.png and favicon.ico (16/32/48)
- apple-touch-icon.png, 180 px, flattened onto the tile colour because iOS paints transparency black
- icon-192.png and icon-512.png

Also prints the wordmark <symbol> markup, with ids prefixed so they don't clash in the page.
Pillow writes PNGs without metadata chunks. Python 3.9+, needs Pillow.
"""
import io
import os
import re
import struct
import sys

from PIL import Image

SITE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(SITE, "assets")
TILE = (21, 18, 17)  # the icon's darkest tile colour, #151211


def png_bytes(im):
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def main(branding):
    svg = open(os.path.join(branding, "icon.svg"), encoding="utf-8").read()
    with open(os.path.join(ASSETS, "favicon.svg"), "w", encoding="utf-8") as f:
        f.write(svg)
    master = Image.open(os.path.join(branding, "icon-master.png")).convert("RGBA")
    pngs = {}
    for s in (16, 32, 48, 192, 512):
        im = master.resize((s, s), Image.LANCZOS)
        name = f"favicon-{s}.png" if s <= 48 else f"icon-{s}.png"
        data = png_bytes(im)
        open(os.path.join(ASSETS, name), "wb").write(data)
        pngs[s] = data
    touch = Image.new("RGBA", (180, 180), TILE + (255,))
    touch.alpha_composite(master.resize((180, 180), Image.LANCZOS))
    open(os.path.join(ASSETS, "apple-touch-icon.png"), "wb").write(png_bytes(touch.convert("RGB")))
    sizes = (16, 32, 48)
    hdr = struct.pack("<HHH", 0, 1, len(sizes))
    off, ents = 6 + 16 * len(sizes), b""
    for s in sizes:
        ents += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(pngs[s]), off)
        off += len(pngs[s])
    open(os.path.join(ASSETS, "favicon.ico"), "wb").write(hdr + ents + b"".join(pngs[s] for s in sizes))
    # wordmark symbol: the SVG's body, ids prefixed
    body = re.sub(r"^.*?<svg[^>]*>|</svg>\s*$", "", svg, flags=re.S).strip()
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    for i in re.findall(r'id="([^"]+)"', body):
        body = body.replace(f'id="{i}"', f'id="apm-{i}"').replace(f"url(#{i})", f"url(#apm-{i})")
    print(re.sub(r"\s*\n\s*", "", body))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
