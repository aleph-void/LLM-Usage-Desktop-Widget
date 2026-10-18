"""Render assets/icon.png and assets/icon.ico for the packaged builds.

PyInstaller turns icon.png into the .app / .exe icon; Inno Setup uses icon.ico.

Draws the same design as assets/llm-usage-widget.svg, so no SVG renderer is needed.
Requires Pillow.
"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
s = SIZE / 256  # the SVG is drawn on a 256 grid

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([8 * s, 8 * s, 248 * s, 248 * s], radius=52 * s, fill="#1c1d21")
for x, fill_top, color in ((56, 96, "#d97757"), (140, 144, "#10a37f")):
    d.rounded_rectangle([x * s, 48 * s, (x + 60) * s, 208 * s], radius=14 * s, fill="#3a3c44")
    # Fill: square top edge, rounded bottom (matches the track's corners).
    d.rounded_rectangle([x * s, fill_top * s, (x + 60) * s, 208 * s], radius=14 * s, fill=color)
    d.rectangle([x * s, fill_top * s, (x + 60) * s, (fill_top + 14) * s], fill=color)

assets = Path(__file__).resolve().parent.parent / "assets"
img.save(assets / "icon.png")
img.save(assets / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(assets / "icon.png")
print(assets / "icon.ico")
