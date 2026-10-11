#!/usr/bin/env python3
"""Makes the page's still images from the delivered film files:

- assets/film/habit-N-poster.webp: one still per habit clip (at 60 % of its length unless AT says otherwise), 800 px wide
- assets/film/poster-800.webp: an 800 px copy of the hero poster for small screens

Metadata is not carried over (ffmpeg -map_metadata -1; Pillow writes no EXIF unless asked).
Needs ffmpeg/ffprobe on PATH and Pillow with WebP.   python3 site/tools/habit-posters.py
"""
import os
import subprocess
import tempfile

from PIL import Image

AT = {4: 0.22}  # where in each clip the still is taken (fraction); habit 4 at its 3D block city
FILM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "film")

with tempfile.TemporaryDirectory() as tmp:
    for i in range(1, 5):
        clip = os.path.join(FILM, f"habit-{i}.webm")
        dur = float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", clip], text=True))
        png = os.path.join(tmp, f"h{i}.png")
        subprocess.check_call(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * AT.get(i, 0.6):.2f}", "-i", clip, "-frames:v", "1",
                               "-vf", "scale=800:-2", "-map_metadata", "-1", png])
        out = os.path.join(FILM, f"habit-{i}-poster.webp")
        Image.open(png).convert("RGB").save(out, "WEBP", quality=72, method=6)
        print(out, os.path.getsize(out))

src = Image.open(os.path.join(FILM, "poster.webp")).convert("RGB")
small = src.resize((800, round(src.height * 800 / src.width)), Image.LANCZOS)
out = os.path.join(FILM, "poster-800.webp")
small.save(out, "WEBP", quality=64, method=6)
print(out, os.path.getsize(out))
