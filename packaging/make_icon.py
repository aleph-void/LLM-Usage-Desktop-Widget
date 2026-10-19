"""Render assets/icon.png and assets/icon.ico for the packaged builds.

PyInstaller turns icon.png into the .app / .exe icon; Inno Setup uses icon.ico.
The .deb and install.sh use assets/llm-usage-widget.svg directly.

Draws the Aleph Void mark with usage_widget.app_icon_image (the "Collapse to logo"
glyph on a violet tile), so no SVG renderer is needed.
Requires Pillow.
"""

import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from usage_widget import app_icon_image  # noqa: E402

assets = root / "assets"
img = app_icon_image(1024)
img.save(assets / "icon.png")
img.save(assets / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(assets / "icon.png")
print(assets / "icon.ico")
