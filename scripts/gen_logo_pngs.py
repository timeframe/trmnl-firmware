#!/usr/bin/env python3
"""Generate 1-bit B&W PNGs from the SVG wordmark for e-paper displays."""
import io
import os
import sys

import cairosvg
from PIL import Image

def main():
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    svg_path = os.path.join(project_dir, "assets", "logo.svg")
    assets_dir = os.path.join(project_dir, "assets")

    specs = [
        ("logo_small.png", 86, 86),
        ("logo_medium.png", 240, 240),
        ("loading.png", 800, 480),
    ]

    with open(svg_path, "rb") as f:
        svg_data = f.read()

    for fname, w, h in specs:
        out_path = os.path.join(assets_dir, fname)

        # Render SVG scaled to fit within canvas with some padding
        max_logo_w = int(w * 0.85)
        max_logo_h = int(h * 0.4)

        png_data = cairosvg.svg2png(bytestring=svg_data, output_width=max_logo_w)
        logo = Image.open(io.BytesIO(png_data)).convert("RGBA")

        # Scale down if taller than allowed
        if logo.height > max_logo_h:
            ratio = max_logo_h / logo.height
            logo = logo.resize(
                (int(logo.width * ratio), max_logo_h), Image.LANCZOS
            )

        # White canvas
        canvas = Image.new("RGB", (w, h), "white")

        # Paste logo centered, using alpha channel as mask
        x = (w - logo.width) // 2
        y = (h - logo.height) // 2
        canvas.paste(
            Image.new("RGB", logo.size, "black"), (x, y), logo.split()[3]
        )

        # Convert to 1-bit and save
        bw = canvas.convert("1")
        bw.save(out_path)
        print(f"wrote {out_path} ({w}x{h})")


if __name__ == "__main__":
    main()
