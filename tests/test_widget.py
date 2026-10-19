"""The floating Tk widget and the taskbar strip's drawing, on a real Tk (skipped when there is no display)."""

from __future__ import annotations

import subprocess
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

import usage_widget as w

tk = pytest.importorskip("tkinter")


# Builds whose Tk can't start here (headers that don't match the library, Tk 9 on a headless
# macOS session) raise RuntimeError or crash the interpreter, so try it in a child process first.
PROBE = "import tkinter\nfor _ in range(3):\n    r = tkinter.Tk(); r.update(); r.destroy()"


@pytest.fixture(scope="module")
def tk_available():
    try:
        probe = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        pytest.skip("Tk hung starting up")
    if probe.returncode:
        pytest.skip(f"no usable Tk: {(probe.stderr.strip().splitlines() or [probe.returncode])[-1]}")


@pytest.fixture(autouse=True)
def retry_tk_startup(monkeypatch):
    """Windows' Tcl now and then fails to source its own init scripts when a Tk follows another one
    in the same process ("Can't find a usable tk.tcl", "invalid command name tcl_findLibrary")."""
    if sys.platform != "win32":
        return

    class Tk(tk.Tk):
        def __init__(self, *args, **kwargs):
            for attempt in range(5):
                try:
                    return super().__init__(*args, **kwargs)
                except tk.TclError:
                    if attempt == 4:
                        raise

    monkeypatch.setattr(tk, "Tk", Tk)


@pytest.fixture(scope="module")
def shared_root(tk_available):
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture(autouse=True)
def one_tk_on_macos(request, monkeypatch):
    """The python.org macOS builds' Tk now and then segfaults in update() on a Tk created after an
    earlier one was destroyed in the same process, so there every window the tests make is a
    Toplevel of one hidden Tk that lives for the whole module."""
    if sys.platform != "darwin":
        yield
        return
    root = request.getfixturevalue("shared_root")
    monkeypatch.setattr(tk, "Tk", lambda *args, **kwargs: tk.Toplevel(root))
    yield
    # A destroyed window's timers (the widget's _tick) would still fire in later tests.
    for after_id in root.tk.splitlist(root.tk.call("after", "info")):
        root.after_cancel(after_id)


@pytest.fixture
def inline_threads(monkeypatch):
    class InlineThread:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(w, "threading", types.SimpleNamespace(Thread=InlineThread, Event=w.threading.Event))


def texts(widget) -> list[str]:
    """Every Label's text under widget, depth first."""
    out = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Label):
            out.append(child.cget("text"))
        out.extend(texts(child))
    return out


def sample():
    soon = datetime.now(timezone.utc) + timedelta(hours=5)
    return [
        w.ProviderUsage("Claude", plan="pro", fetched_at=1_700_000_000,
                        limits=[w.Limit("Session (5h)", 95, soon), w.Limit("Weekly", 0)]),
        w.ProviderUsage("Codex", error="HTTP 500", fetched_at=1_700_000_000),
        w.ProviderUsage("Bedrock", summary="$1.00", fetched_at=1_700_000_000,
                        limits=[w.Limit("This month $1.00", None, soon)]),
    ]


@pytest.fixture
def widget(tk_available, inline_threads, monkeypatch):
    monkeypatch.setattr(w, "fetch_all", sample)
    widget = w.Widget(5, framed=True)
    widget.root.update()  # run the after(0) that applies the first fetch
    yield widget
    try:
        widget.root.destroy()
    except tk.TclError:
        pass


def test_widget_first_render(widget):
    assert widget.interval == w.MIN_INTERVAL
    assert not widget.fetching
    labels = texts(widget.body)
    assert labels[:3] == ["●", "Claude", "Pro"]
    assert "Session (5h)" in labels and "95%" in labels
    assert "Weekly" in labels and "0%" in labels
    assert any(t.startswith("resets in 4h 59m  ·  ") for t in labels)
    assert "⚠ HTTP 500" in labels
    assert "This month $1.00" in labels
    assert widget.status.cget("text").startswith("Updated ")
    assert widget.root.title() == "LLM Usage"


def test_widget_bars(widget):
    rows = [c for c in widget.body.winfo_children()[0].winfo_children() if isinstance(c, tk.Frame)]
    bars = [g for f in rows for g in f.winfo_children() if isinstance(g, tk.Canvas)]
    full, empty = bars
    assert len(full.find_all()) == 2  # track + fill
    assert full.itemcget(full.find_all()[1], "fill") == w.THEME["critical"]
    assert len(empty.find_all()) == 1  # 0%: track only


def test_widget_keeps_stale_numbers(widget):
    widget._apply([w.ProviderUsage("Claude", error="network: down", fetched_at=2)])
    labels = texts(widget.body)
    assert "⚠ stale — network: down" in labels
    assert "95%" in labels
    assert widget.data[0].plan == "pro"


def test_widget_refresh_ignored_while_fetching(widget, monkeypatch):
    widget.fetching = True
    monkeypatch.setattr(w, "fetch_all", lambda: pytest.fail("fetched twice"))
    widget.refresh()


def test_widget_tick(widget, monkeypatch):
    calls = []
    monkeypatch.setattr(widget, "refresh", lambda: calls.append("refresh"))
    monkeypatch.setattr(widget, "render", lambda: calls.append("render"))
    for u in widget.data:
        u.fetched_at = w.time.time()
    widget._tick()
    assert calls == ["render"]
    for u in widget.data:
        u.fetched_at = 0
    widget._tick()
    assert calls == ["render", "refresh"]


def test_widget_drag_saves_position(widget, monkeypatch):
    # Where the window lands is up to the window manager, and moving a mapped window aborts the
    # interpreter with the Tk in python.org's macOS arm64 3.10 build, so stand in for the move.
    pos = {"x": 100, "y": 120}
    moves = []

    def geometry(spec):
        moves.append(spec)
        pos["x"], pos["y"] = (int(v) for v in spec.lstrip("+").split("+"))

    monkeypatch.setattr(widget.root, "geometry", geometry)
    monkeypatch.setattr(widget.root, "winfo_x", lambda: pos["x"])
    monkeypatch.setattr(widget.root, "winfo_y", lambda: pos["y"])
    widget._drag_start(types.SimpleNamespace(x_root=110, y_root=130))  # grabbed 10 px in
    widget._drag_move(types.SimpleNamespace(x_root=160, y_root=170))
    assert moves == ["+150+160"]
    widget._drag_end(None)
    cfg = w.load_config()
    assert (cfg["x"], cfg["y"]) == (150, 160)
    widget._drag_move(types.SimpleNamespace(x_root=500, y_root=500))  # not dragging: ignored
    widget._drag_end(None)  # a second release without a drag changes nothing
    assert moves == ["+150+160"]
    assert (w.load_config()["x"], w.load_config()["y"]) == (150, 160)


def test_widget_toggle_topmost(widget, monkeypatch):
    # Read back, -topmost is unreliable without a window manager (e.g. Xvfb), so check the call.
    calls = []
    monkeypatch.setattr(widget.root, "attributes", lambda *a: calls.append(a))
    widget.topmost_var.set(False)
    widget._toggle_topmost()
    assert w.load_config()["topmost"] is False
    assert calls == [("-topmost", False)]


def test_widget_restores_position(tk_available, inline_threads, monkeypatch):
    monkeypatch.setattr(w, "fetch_all", lambda: [])
    w.save_config({"x": 40, "y": 50, "topmost": False})
    widget = w.Widget(60, framed=True)
    try:
        assert widget.root.geometry().endswith("+40+50")
        assert not widget.topmost_var.get()
        assert texts(widget.body) == []
    finally:
        widget.quit()


def menu_labels(menu) -> list[str]:
    return [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) != "separator"]


def test_widget_menu(widget):
    widget._build_menu()  # built on each popup
    assert menu_labels(widget.menu) == ["Refresh now", "Always on top", "Stop Claude at: off", "Stop Codex at: off",
                                        "Stop Bedrock at: off", "About", "Quit"]


def test_widget_update_available(newer_release, widget, monkeypatch):
    opened = []
    monkeypatch.setattr(w.webbrowser, "open", opened.append)
    assert widget.update == newer_release
    assert widget.update_link.winfo_manager() == "pack"
    assert widget.update_link.cget("text") == "⬆ v99.0.0"
    widget._build_menu()
    assert menu_labels(widget.menu)[:2] == ["Update available: v99.0.0", "Refresh now"]
    assert widget.menu.type(1) == "separator"
    widget.menu.invoke(0)
    widget._open_update()  # what clicking the header link does
    assert opened == [newer_release["url"]] * 2
    widget._apply(sample())  # no newer release (any more)
    assert widget.update_link.winfo_manager() == ""
    widget._build_menu()
    assert menu_labels(widget.menu)[0] == "Refresh now"


def test_widget_no_update(widget):
    # The conftest blocks the network, so the check fails quietly and nothing is offered.
    assert widget.update is None
    assert widget.update_link.winfo_manager() == ""
    widget._build_menu()
    assert menu_labels(widget.menu)[0] == "Refresh now"


def test_widget_cutoff_menu(widget):
    w.save_config({"cutoff": {"Codex": 90}, "bedrock": {"budget": "b"}})
    widget.cfg = w.load_config()
    widget._build_menu()
    menu = widget.menu
    assert menu.entrycget(3, "label") == "Stop Codex at: 90%"
    codex = widget.root.nametowidget(menu.entrycget(3, "menu"))
    assert menu_labels(codex) == ["Off", *(f"{v}%" for v in range(5, 100, 5))]  # no usage known
    assert widget.cutoff_vars[1].get() == 90
    claude = widget.root.nametowidget(menu.entrycget(2, "menu"))
    assert menu_labels(claude) == ["Off"]  # already at 95%
    codex.invoke(19)  # 95%
    claude.invoke(0)  # Off
    assert w.load_config() == {"cutoff": {"Codex": 95}, "bedrock": {"budget": "b"}}
    assert widget.cfg == w.load_config()
    widget._build_menu()  # rebuilding doesn't pile up old submenus
    assert len(widget.menu.winfo_children()) == 4  # three providers and About
    assert widget.menu.entrycget(3, "label") == "Stop Codex at: 95%"


def test_widget_credit_cutoff_menu(widget):
    widget._build_menu()
    assert not any(" at extra usage" in lab or " at credits left" in lab for lab in menu_labels(widget.menu))
    widget.data[0].credits = w.Credits(used=12.5, cap=50, unit="$", what="extra usage")
    w.save_config({"credit_cutoff": {"Claude": 30}})
    widget.cfg = w.load_config()
    widget._build_menu()
    menu = widget.menu
    labels = menu_labels(menu)
    assert labels[2:5] == ["Stop Claude at: off", "Stop Claude at extra usage: $30", "Stop Codex at: off"]
    claude = widget.root.nametowidget(menu.entrycget(3, "menu"))
    assert menu_labels(claude) == ["Off", "$15", "$20", "$30"]
    assert widget.cutoff_vars[1].get() == 30
    claude.invoke(1)  # $15
    assert w.load_config() == {"credit_cutoff": {"Claude": 15}}
    assert widget.cfg == w.load_config()
    widget._build_menu()
    assert menu_labels(widget.menu)[3] == "Stop Claude at extra usage: $15"
    assert len(widget.menu.winfo_children()) == 5  # three providers, one credit cutoff and About


def test_widget_reloads_settings_after_a_fetch(widget):
    w.update_config(cutoff={"Claude": 99})  # e.g. changed on another computer; above the sample's 95%
    widget.refresh()
    widget.root.update()
    assert widget.cfg["cutoff"] == {"Claude": 99}


def test_widget_picks_up_a_reached_cutoff(widget, monkeypatch):
    monkeypatch.setattr(w, "find_cli_processes", lambda: [])

    def fresh():  # fetched now, so a _tick firing on a slow runner redraws instead of fetching again
        results = sample()
        for u in results:
            u.fetched_at = time.time()
        return results

    monkeypatch.setattr(w, "fetch_all", fresh)
    w.update_config(cutoff={"Claude": 90}, x=7)  # the sample's Claude is at 95%
    widget.refresh()
    widget.root.update()
    assert "cutoff" not in widget.cfg  # cleared once reached, so a later drag can't save it back
    assert widget.cfg["x"] == 7
    assert any(t.startswith("⛔ Cutoff 90% reached") for t in texts(widget.body))


# -- TaskbarStrip._draw ----------------------------------------------------------

@pytest.fixture
def strip(tk_available):
    import tkinter.font as tkfont

    root = tk.Tk()
    s = object.__new__(w.TaskbarStrip)
    s.tk, s.root, s.tray = tk, root, types.SimpleNamespace(open_menu=lambda: s.clicks.append(1))
    s.clicks = []
    s.fonts = {"line": (tkfont.Font(size=9), tkfont.Font(size=9, weight="bold")),
               "stacked": (tkfont.Font(size=8), tkfont.Font(size=8, weight="bold"))}
    s.pad, s.bg, s.shown_key = 4, "#1c1c1c", None
    s.frame = tk.Frame(root)
    s.frame.pack()
    s.inner = tk.Frame(s.frame)
    s.inner.pack()
    yield s
    root.destroy()


def test_strip_draw_dark_taskbar(strip):
    parts = [("Claude", "95%", "critical"), ("Codex", "10%", "normal")]
    strip._draw("#202020", parts, "line")
    labels = strip.inner.winfo_children()
    assert [lab.cget("text") for lab in labels] == ["Claude", "95%", "Codex", "10%"]
    assert labels[0].cget("fg") == "#ffffff"
    assert labels[1].cget("fg") == w.THEME["critical"]
    assert labels[3].cget("fg") == "#ffffff"
    assert strip.frame.cget("bg") == "#202020"
    assert strip._width(parts, "line") > strip._width(parts, "stacked")


def test_strip_draw_light_taskbar_stacked(strip):
    strip._draw("#f0f0f0", [("Claude", "80%", "warning"), ("Bedrock", "–", "muted")], "stacked")
    labels = strip.inner.winfo_children()
    assert labels[0].cget("fg") == "#000000"
    assert labels[1].cget("fg") == "#a86b00"
    assert labels[3].cget("fg") == "#5f6068"
    assert labels[0].grid_info()["row"] == 0 and labels[2].grid_info()["row"] == 1


def test_strip_draw_skips_unchanged(strip):
    parts = [("Claude", "1%", "normal")]
    strip._draw("#202020", parts, "line")
    first = strip.inner.winfo_children()
    strip._draw("#202020", parts, "line")
    assert strip.inner.winfo_children() == first


def test_strip_click_opens_menu(strip):
    strip._draw("#202020", [("Claude", "1%", "normal")], "line")
    strip._click()
    assert strip.clicks == [1]
