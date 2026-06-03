#!/usr/bin/env python3
"""Generate an SVG wordmark from a font file. Used once to create assets/logo.svg."""
import os
import sys
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen


def main():
    font_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/lato-bold.woff2"
    text = sys.argv[2] if len(sys.argv) > 2 else "timeframe"
    fill = sys.argv[3] if len(sys.argv) > 3 else "#333333"
    out_path = sys.argv[4] if len(sys.argv) > 4 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets", "logo.svg",
    )

    font = TTFont(font_path)
    cmap = font.getBestCmap()
    glyphset = font.getGlyphSet()
    upm = font["head"].unitsPerEm

    target_height = 24  # desired em height in SVG px
    scale = target_height / upm

    glyphs = []
    x_cursor = 0
    for ch in text:
        glyph_name = cmap.get(ord(ch))
        if not glyph_name:
            continue
        pen = SVGPathPen(glyphset)
        glyphset[glyph_name].draw(pen)
        path_d = pen.getCommands()
        advance = font["hmtx"][glyph_name][0]
        if path_d:
            glyphs.append((x_cursor, path_d))
        x_cursor += advance

    total_width = x_cursor * scale
    margin_x = 2
    margin_top = 4
    margin_bottom = 4
    svg_w = total_width + margin_x * 2
    svg_h = target_height + margin_top + margin_bottom

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{svg_w:.1f}" height="{svg_h:.1f}" '
        f'viewBox="0 0 {svg_w:.1f} {svg_h:.1f}">',
    ]
    baseline_y = target_height + margin_top
    for xoff, path_d in glyphs:
        tx = xoff * scale + margin_x
        lines.append(
            f'  <path d="{path_d}" fill="{fill}" '
            f'transform="translate({tx:.2f},{baseline_y:.2f}) '
            f'scale({scale:.6f},{-scale:.6f})"/>'
        )
    lines.append("</svg>")
    lines.append("")

    svg = "\n".join(lines)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"wrote {out_path}  ({len(svg)} bytes)")


if __name__ == "__main__":
    main()
