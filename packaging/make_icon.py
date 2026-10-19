"""Render assets/icon.png and assets/icon.ico for the packaged builds.

PyInstaller turns icon.png into the .app / .exe icon; Inno Setup uses icon.ico.
The .deb and install.sh use assets/llm-usage-widget.svg directly.

Reads the shapes from assets/llm-usage-widget.svg (the Aleph Void mark: one rounded
tile, one straight-line path and one stroked ellipse), so no SVG renderer is needed.
Requires Pillow.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
SUPERSAMPLE = 4  # Pillow's shapes aren't anti-aliased; draw big and scale down
s = SIZE * SUPERSAMPLE / 256  # the SVG is drawn on a 256 grid

assets = Path(__file__).resolve().parent.parent / "assets"
svg = {el.tag.split("}")[1]: el.attrib for el in ET.parse(assets / "llm-usage-widget.svg").getroot()}
num = lambda a, key: float(a[key]) * s  # noqa: E731

img = Image.new("RGBA", (SIZE * SUPERSAMPLE,) * 2, (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Tile: the SVG stroke is centred on the rect's edge; Pillow draws its outline inside the box.
r, half = svg["rect"], float(svg["rect"]["stroke-width"]) / 2
x, y, w, h = (float(r[k]) for k in ("x", "y", "width", "height"))
d.rounded_rectangle([(x - half) * s, (y - half) * s, (x + w + half) * s, (y + h + half) * s],
                    radius=(float(r["rx"]) + half) * s, fill=r["fill"], outline=r["stroke"],
                    width=round(float(r["stroke-width"]) * s))

# Aleph: "M x y L x y ... Z".
p = svg["path"]
points = [(float(a) * s, float(b) * s) for a, b in re.findall(r"([\d.]+) ([\d.]+)", p["d"])]
d.polygon(points, fill=p["fill"])

# Subscript zero: a stroked ellipse.
e = svg["ellipse"]
cx, cy, rx, ry, sw = (num(e, k) for k in ("cx", "cy", "rx", "ry", "stroke-width"))
d.ellipse([cx - rx - sw / 2, cy - ry - sw / 2, cx + rx + sw / 2, cy + ry + sw / 2],
          outline=e["stroke"], width=round(sw))

img = img.resize((SIZE, SIZE), Image.LANCZOS)
img.save(assets / "icon.png")
img.save(assets / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(assets / "icon.png")
print(assets / "icon.ico")
