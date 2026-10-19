"""Things the build scripts and installers rely on."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

import usage_widget as w
from conftest import ROOT

# build-deb.sh, build-pkg.sh and build.ps1 all pull the version out with this pattern.
VERSION_LINE = re.compile(r'^__version__ = "(.*)"$', re.MULTILINE)


def test_version_line_is_where_the_build_scripts_look():
    found = VERSION_LINE.findall((ROOT / "usage_widget.py").read_text(encoding="utf-8"))
    assert found == [w.__version__]


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", w.__version__)
    assert w.USER_AGENT == f"llm-usage-widget/{w.__version__}"


def test_build_scripts_read_the_version_the_same_way():
    for script in ("packaging/linux/build-deb.sh", "packaging/macos/build-pkg.sh"):
        assert r's/^__version__ = "\(.*\)"/\1/p' in (ROOT / script).read_text(encoding="utf-8"), script
    assert "'^__version__ = \"(.*)\"'" in (ROOT / "packaging/windows/build.ps1").read_text(encoding="utf-8")


def test_windows_uninstall_switch_looks_up_the_installers_app_id():
    iss = (ROOT / "packaging/windows/installer.iss").read_text(encoding="utf-8")
    app_id = re.search(r"^AppId=\{(\{[0-9A-F-]+\})$", iss, re.MULTILINE).group(1)
    assert f'#define AppIdGuid "{app_id}"' in iss


@pytest.mark.parametrize("level", list(w.ICON_COLORS))
def test_top_bar_icons_are_valid_svg(level):
    c1, c2, c3 = w.ICON_COLORS[level]
    root = ET.fromstring(w.ICON_SVG.format(c1=c1, c2=c2, c3=c3))
    assert root.tag.endswith("svg")
    assert [rect.get("fill") for rect in root] == [c1, c2, c3]


def test_app_svg_is_valid():
    root = ET.parse(ROOT / "assets" / "llm-usage-widget.svg").getroot()
    assert root.tag.endswith("svg")


def test_theme_has_a_color_for_every_provider_and_level():
    for name, _ in w.PROVIDERS:
        assert name.lower() in w.THEME
        assert name in w.USAGE_PAGES
    for level in ("normal", "warning", "critical"):
        assert re.fullmatch(r"#[0-9a-f]{6}", w.THEME[level])


def test_make_icon(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    (tmp_path / "packaging").mkdir()
    (tmp_path / "assets").mkdir()
    shutil.copy(ROOT / "packaging" / "make_icon.py", tmp_path / "packaging" / "make_icon.py")
    shutil.copy(ROOT / "assets" / "llm-usage-widget.svg", tmp_path / "assets" / "llm-usage-widget.svg")
    out = subprocess.run([sys.executable, str(tmp_path / "packaging" / "make_icon.py")], capture_output=True,
                         text=True, timeout=120, check=False)
    assert out.returncode == 0, out.stderr
    png = Image.open(tmp_path / "assets" / "icon.png")
    assert png.size == (1024, 1024)
    ico = Image.open(tmp_path / "assets" / "icon.ico")
    assert (256, 256) in ico.info["sizes"] and (16, 16) in ico.info["sizes"]
