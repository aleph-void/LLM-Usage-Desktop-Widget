"""Tray front ends with their toolkits faked: Linux AppIndicator, pystray (macOS / Windows) and the tray icon."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

import usage_widget as w


@pytest.fixture
def sync_threads(monkeypatch):
    """Run background threads inline so a refresh completes before the test looks."""

    class InlineThread:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    fake = types.SimpleNamespace(Thread=InlineThread, Event=w.threading.Event)
    monkeypatch.setattr(w, "threading", fake)


def sample_data():
    soon = datetime.now(timezone.utc) + timedelta(hours=3)
    return [
        w.ProviderUsage("Claude", plan="max", fetched_at=1.0,
                        limits=[w.Limit("Session (5h)", 92, soon), w.Limit("Weekly", 40)]),
        w.ProviderUsage("Codex", plan="plus", fetched_at=1.0, limits=[w.Limit("5h", 80)]),
        w.ProviderUsage("Bedrock", summary="$5.00", fetched_at=1.0,
                        limits=[w.Limit("This month $5.00", None, soon)]),
    ]


# ==============================================================================
# Linux: AppIndicator via PyGObject
# ==============================================================================

class FakeMenuItem:
    def __init__(self, label=None):
        self.label = label
        self.sensitive = True
        self.handlers = {}
        self.submenu = None

    def set_sensitive(self, value):
        self.sensitive = value

    def connect(self, signal, callback):
        self.handlers[signal] = callback

    def set_submenu(self, menu):
        self.submenu = menu


class FakeSeparator(FakeMenuItem):
    pass


class FakeCheckMenuItem(FakeMenuItem):
    def __init__(self, label=None):
        super().__init__(label)
        self.active = False
        self.radio = False

    def set_draw_as_radio(self, value):
        self.radio = value

    def set_active(self, value):
        self.active = value

    def connect(self, signal, callback, *args):
        self.handlers[signal] = lambda item: callback(item, *args)


class FakeMenu:
    def __init__(self):
        self.items = []
        self.shown = False

    def append(self, item):
        self.items.append(item)

    def show_all(self):
        self.shown = True

    def labels(self):
        return [None if isinstance(i, FakeSeparator) else i.label for i in self.items]

    def item(self, label):
        return next(i for i in self.items if i.label == label)


class FakeIndicator:
    def __init__(self, *args):
        self.args = args
        self.label = None
        self.icon = None
        self.menu = None
        self.theme_path = None

    def set_icon_theme_path(self, path):
        self.theme_path = path

    def set_status(self, status):
        self.status = status

    def set_title(self, title):
        self.title = title

    def set_label(self, text, guide):
        self.label = text

    def set_icon_full(self, name, desc):
        self.icon = name

    def set_menu(self, menu):
        self.menu = menu


@pytest.fixture
def gtk(monkeypatch):
    opened, timers, idle = [], [], []
    Gtk = types.SimpleNamespace(
        Menu=FakeMenu, MenuItem=FakeMenuItem, SeparatorMenuItem=FakeSeparator, CheckMenuItem=FakeCheckMenuItem,
        show_uri_on_window=lambda win, uri, t: opened.append(uri), get_current_event_time=lambda: 0,
        main_quit=lambda: opened.append("quit"), main=lambda: None,
    )
    GLib = types.SimpleNamespace(timeout_add_seconds=lambda s, fn: timers.append((s, fn)),
                                 idle_add=lambda fn, *a: idle.append((fn, a)))
    AI = types.SimpleNamespace(
        Indicator=types.SimpleNamespace(new=FakeIndicator),
        IndicatorCategory=types.SimpleNamespace(APPLICATION_STATUS="app-status"),
        IndicatorStatus=types.SimpleNamespace(ACTIVE="active"),
    )
    repo = types.ModuleType("gi.repository")
    repo.Gtk, repo.GLib = Gtk, GLib
    gi = types.ModuleType("gi")
    gi.repository = repo
    gi.require_version = lambda name, version: None
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)
    return types.SimpleNamespace(Gtk=Gtk, GLib=GLib, AI=AI, opened=opened, timers=timers, idle=idle, gi=gi,
                                 repo=repo)


def run_idle(gtk):
    while gtk.idle:
        fn, args = gtk.idle.pop(0)
        fn(*args)


@pytest.fixture
def linux_tray(gtk, sync_threads, monkeypatch):
    data = sample_data()
    monkeypatch.setattr(w, "fetch_all", lambda: [w.ProviderUsage(u.name, u.plan, list(u.limits), u.error,
                                                                 u.fetched_at, u.summary) for u in data])
    tray = w.Tray(5, gtk.AI)
    return tray


def test_tray_startup(gtk, sync_threads, monkeypatch):
    monkeypatch.setattr(w, "fetch_all", lambda: [])
    tray = w.Tray(5, gtk.AI)
    assert tray.interval == w.MIN_INTERVAL
    assert tray.ind.args == (w.APP_NAME, "llm-usage-normal", "app-status")
    assert tray.ind.theme_path == str(w.config_dir() / "icons")
    for level in w.ICON_COLORS:
        svg = (w.config_dir() / "icons" / f"llm-usage-{level}.svg").read_text()
        assert svg.startswith("<svg") and "{c1}" not in svg
    assert tray.ind.menu.labels()[0] == "Loading…"
    assert gtk.timers and gtk.timers[0][0] == 30
    assert tray.fetching  # the first refresh waits on the main loop


def test_tray_render_after_fetch(linux_tray, gtk):
    run_idle(gtk)
    tray = linux_tray
    assert not tray.fetching
    assert tray.ind.label == "Claude 92%  Codex 80%  Bedrock $5.00"
    assert tray.ind.icon == "llm-usage-critical"
    labels = tray.ind.menu.labels()
    assert labels[0] == "Claude  (Max)"
    assert "    Session (5h):  92%  ●" in labels
    assert any(lab.startswith("    █████████░  resets in 2h 59m") for lab in labels if lab)
    assert "Codex  (Plus)" in labels
    assert "    5h:  80%  ▲" in labels
    assert any(lab.startswith("    This month $5.00  ·  resets in") for lab in labels if lab)
    assert {"Refresh now", "Open Claude usage page", "Open Codex usage page", "Open Bedrock usage page", "About",
            "Quit"} <= set(labels)
    assert tray.ind.menu.shown


def test_tray_menu_actions(linux_tray, gtk):
    run_idle(gtk)
    menu = linux_tray.ind.menu
    menu.item("Open Codex usage page").handlers["activate"](None)
    about = menu.item("About").submenu
    assert about.items[0].label == f"LLM Usage Widget — Version {w.__version__}"
    assert not about.items[0].sensitive
    about.items[1].handlers["activate"](None)
    menu.item("Quit").handlers["activate"](None)
    assert gtk.opened == [w.USAGE_PAGES["Codex"], w.REPO_URL, "quit"]


def test_tray_cutoff_menu(linux_tray, gtk):
    run_idle(gtk)
    w.save_config({"cutoff": {"Codex": 95}})
    linux_tray.render()
    menu = linux_tray.ind.menu
    assert {"Stop Claude at: off", "Stop Codex at: 95%", "Stop Bedrock at: off"} <= set(menu.labels())
    codex = menu.item("Stop Codex at: 95%").submenu
    assert codex.labels() == ["Off", "85%", "90%", "95%"]  # Codex is at 80%
    assert [i.active for i in codex.items] == [False, False, False, True]
    assert all(i.radio for i in codex.items)
    menu.item("Stop Claude at: off").submenu.item("95%").handlers["activate"](None)
    codex.item("Off").handlers["activate"](None)
    assert w.load_config() == {"cutoff": {"Claude": 95}}
    run_idle(gtk)  # re-rendered after the menu closes
    assert "Stop Claude at: 95%" in linux_tray.ind.menu.labels()


def test_tray_credit_cutoff_menu(linux_tray, gtk):
    run_idle(gtk)
    linux_tray.render()
    labels = linux_tray.ind.menu.labels()
    assert not any(" at extra usage" in (lab or "") or " at credits left" in (lab or "") for lab in labels)
    # Claude reports extra usage and Codex a credit balance: each gets a credit cutoff after its percentage.
    linux_tray.data[0].credits = w.Credits(used=12.5, cap=50, unit="$", what="extra usage")
    linux_tray.data[1].credits = w.Credits(left=42, what="credits left")
    w.save_config({"credit_cutoff": {"Codex": 20}})
    linux_tray.render()
    menu = linux_tray.ind.menu
    labels = menu.labels()
    assert labels.index("Stop Claude at: off") + 1 == labels.index("Stop Claude at extra usage: off")
    assert labels.index("Stop Codex at: off") + 1 == labels.index("Stop Codex at credits left: 20")
    assert "Stop Bedrock at: off" in labels and not any("Stop Bedrock at " in (lab or "") for lab in labels)
    claude = menu.item("Stop Claude at extra usage: off").submenu
    assert claude.labels() == ["Off", "$15", "$20", "$30"]  # above $12.50 spent, below the $50 cap
    codex = menu.item("Stop Codex at credits left: 20").submenu
    assert codex.labels() == ["Off", "1", "1.5", "2", "3", "5", "7.5", "10", "15", "20", "30"]  # below 42 left
    assert [i.active for i in codex.items] == [False] * 9 + [True, False]
    assert all(i.radio for i in claude.items + codex.items)
    claude.item("$20").handlers["activate"](None)
    codex.item("Off").handlers["activate"](None)
    assert w.load_config() == {"credit_cutoff": {"Claude": 20}}
    run_idle(gtk)
    assert "Stop Claude at extra usage: $20" in linux_tray.ind.menu.labels()


@pytest.fixture
def notify_send(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(w.shutil, "which", lambda name, **kw: f"/usr/bin/{name}")
    monkeypatch.setattr(w.subprocess, "run", lambda args, **kw: calls.append(args))
    return calls


def test_tray_update_available(notify_send, newer_release, linux_tray, gtk, fake_http):
    run_idle(gtk)
    tray = linux_tray
    assert [args[0] for args in notify_send] == ["notify-send"]
    labels = tray.ind.menu.labels()
    assert labels[:3] == ["Update available: v99.0.0", None, "Claude  (Max)"]
    assert tray.ind.title == "LLM Usage — Update available: v99.0.0"
    tray.ind.menu.item("Update available: v99.0.0").handlers["activate"](None)
    assert gtk.opened == [newer_release["url"]]
    tray.refresh()
    run_idle(gtk)
    assert len(fake_http.urls(w.RELEASES_API)) == 1  # the answer is cached, not fetched on every refresh
    assert len(notify_send) == 1  # and announced once
    assert tray.ind.menu.labels()[0] == "Update available: v99.0.0"
    tray._apply(sample_data())  # no newer release (any more)
    assert "Update available: v99.0.0" not in tray.ind.menu.labels()
    assert tray.ind.title == "LLM Usage"


def test_tray_update_check_disabled(newer_release, gtk, sync_threads, fake_http, monkeypatch):
    w.save_config({"update_check": False})
    monkeypatch.setattr(w, "fetch_all", sample_data)
    tray = w.Tray(60, gtk.AI)
    run_idle(gtk)
    assert tray.ind.menu.labels()[0] == "Claude  (Max)"
    assert fake_http.calls == []


def test_tray_icon_levels(linux_tray, gtk):
    run_idle(gtk)
    tray = linux_tray
    tray.data = [w.ProviderUsage("Codex", limits=[w.Limit("5h", 76)])]
    tray.render()
    assert tray.ind.icon == "llm-usage-warning"
    tray.data = [w.ProviderUsage("Codex", limits=[w.Limit("5h", 10)])]
    tray.render()
    assert tray.ind.icon == "llm-usage-normal"
    tray.data = [w.ProviderUsage("Claude", error="HTTP 500"), w.ProviderUsage("Codex", error="nope")]
    tray.render()
    assert tray.ind.icon == "llm-usage-error"
    assert tray.ind.label == "Claude –  Codex –"


def test_tray_shows_credits_left_once_the_plan_is_full(linux_tray, gtk):
    run_idle(gtk)
    tray = linux_tray
    tray.data = [w.ProviderUsage("Claude", limits=[w.Limit("5h", 100)], credits=w.Credits(used=10, cap=40, unit="$")),
                 w.ProviderUsage("Codex", limits=[w.Limit("5h", 100)], credits=w.Credits(left=42))]
    tray.render()
    assert tray.ind.label == "Claude 75% left  Codex 42 left"
    assert tray.ind.icon == "llm-usage-normal"
    tray.data[0].credits.used = 38  # 5% left
    tray.render()
    assert tray.ind.label == "Claude 5% left  Codex 42 left"
    assert tray.ind.icon == "llm-usage-critical"
    tray.data[0].limits[0].percent = 99  # the plan has room again: back to its percentage
    tray.render()
    assert tray.ind.label == "Claude 99%  Codex 42 left"


def test_tray_collapse_to_logo(linux_tray, gtk):
    run_idle(gtk)
    tray = linux_tray
    assert (w.config_dir() / "icons" / "llm-usage-logo.svg").read_text() == w.LOGO_SVG
    item = tray.ind.menu.item(w.COLLAPSE_LABEL)
    assert not item.active
    item.handlers["activate"](item)
    run_idle(gtk)  # re-rendered after the menu closes
    assert w.load_config() == {"collapsed": True}
    assert (tray.ind.label, tray.ind.icon) == ("", "llm-usage-logo")
    assert tray.ind.menu.item(w.COLLAPSE_LABEL).active
    tray.ind.menu.item(w.COLLAPSE_LABEL).handlers["activate"](None)
    run_idle(gtk)
    assert (tray.ind.label, tray.ind.icon) == ("Claude 92%  Codex 80%  Bedrock $5.00", "llm-usage-critical")


def test_tray_keeps_stale_numbers(linux_tray, gtk):
    run_idle(gtk)
    tray = linux_tray
    tray._apply([w.ProviderUsage("Claude", error="network: down"), w.ProviderUsage("Codex", error="HTTP 401")])
    claude, codex = tray.data
    assert [lim.label for lim in claude.limits] == ["Session (5h)", "Weekly"]
    assert claude.plan == "max"
    assert "⚠ stale — network: down" in tray.ind.menu.labels()
    assert tray.ind.label.startswith("Claude 92%")


def test_tray_tick(linux_tray, gtk, monkeypatch):
    run_idle(gtk)
    tray = linux_tray
    renders = []
    monkeypatch.setattr(tray, "render", lambda: renders.append(1))
    assert tray._tick() is True
    assert renders == [1] and not gtk.idle  # too soon: just redraw countdowns
    tray.last_fetch -= tray.interval
    assert tray._tick() is True
    assert gtk.idle  # refreshed
    tray.fetching = True
    tray.last_fetch = 0
    tray.refresh()  # ignored while a fetch is running
    assert len(gtk.idle) == 1


def test_load_appindicator_prefers_ayatana(gtk):
    gtk.repo.AyatanaAppIndicator3 = "ayatana"
    gtk.repo.AppIndicator3 = "classic"
    assert w.load_appindicator() == "ayatana"


def test_load_appindicator_classic(gtk):
    def require(name, version):
        if name == "AyatanaAppIndicator3":
            raise ValueError("Namespace not available")

    gtk.gi.require_version = require
    gtk.repo.AppIndicator3 = "classic"
    assert w.load_appindicator() == "classic"


def test_load_appindicator_missing_typelibs(gtk):
    def require(name, version):
        if name != "Gtk":
            raise ValueError("Namespace not available")

    gtk.gi.require_version = require
    assert w.load_appindicator() is None


def test_load_appindicator_without_gtk(gtk):
    gtk.gi.require_version = lambda name, version: (_ for _ in ()).throw(ValueError("no Gtk"))
    assert w.load_appindicator() is None


def test_load_appindicator_without_pygobject(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)
    assert w.load_appindicator() is None


# ==============================================================================
# macOS / Windows: pystray
# ==============================================================================

class FakeItem:
    def __init__(self, text, action, enabled=True, checked=None, radio=False):
        self.text, self.action, self.enabled, self.checked, self.radio = text, action, enabled, checked, radio


class FakePystrayMenu:
    SEPARATOR = "---"

    def __init__(self, *items):
        self.items = list(items)

    def texts(self):
        return [i if i == self.SEPARATOR else i.text for i in self.items]

    def item(self, text):
        return next(i for i in self.items if i != self.SEPARATOR and i.text == text)


class FakeIcon:
    default_handlers: dict = {}

    def __init__(self, name, icon, title, menu):
        self.name, self.icon, self.title, self.menu = name, icon, title, menu
        self.stopped = False
        self._message_handlers = dict(self.default_handlers)

    def stop(self):
        self.stopped = True


@pytest.fixture
def pystray(monkeypatch):
    mod = types.ModuleType("pystray")
    mod.Icon, mod.Menu, mod.MenuItem = FakeIcon, FakePystrayMenu, FakeItem
    monkeypatch.setitem(sys.modules, "pystray", mod)
    return mod


@pytest.fixture
def desktop(pystray, monkeypatch):
    pytest.importorskip("PIL")
    monkeypatch.setattr(sys, "platform", "linux")  # neither the Windows nor the macOS extras
    return w.DesktopTray(10)


def test_desktop_tray_startup(desktop):
    assert desktop.interval == w.MIN_INTERVAL
    assert desktop.icon.name == w.APP_NAME
    assert desktop.icon.icon.size == (64, 64)
    assert desktop.icon.menu.texts()[0] == "Loading…"
    assert desktop.strip is None


def test_desktop_tray_render(desktop, monkeypatch):
    opened = []
    monkeypatch.setattr(w.webbrowser, "open", opened.append)
    desktop.last_fetch = 1.0
    desktop._apply(sample_data())
    assert desktop.icon.title == "Claude 92%  Codex 80%  Bedrock $5.00"
    texts = desktop.icon.menu.texts()
    assert texts[0] == "Claude  (Max)"
    assert texts[1].startswith("    Session (5h):  92%  ●   ·  resets in ")
    assert texts[2] == "    Weekly:  40%"
    assert texts[3] == "---"
    assert "    5h:  80%  ▲" in texts
    assert any(t.startswith("    This month $5.00   ·  resets in") for t in texts if t != "---")
    assert any(t.startswith("Updated ") for t in texts if t != "---")
    assert "Show usage on the taskbar" not in texts  # Windows only
    desktop.icon.menu.item("Open Claude usage page").action()
    desktop.icon.menu.item("Open Codex usage page").action()
    about = desktop.icon.menu.item("About").action
    about.item(f"Aleph Void LLC: {w.LLC_URL}").action()
    assert opened == [w.USAGE_PAGES["Claude"], w.USAGE_PAGES["Codex"], w.LLC_URL]


def test_desktop_tray_cutoff_menu(desktop):
    desktop._apply(sample_data())
    w.update_config(cutoff={"Claude": 95})  # set elsewhere: picked up on the next fetch
    desktop._apply(sample_data())
    assert {"Stop Claude at: 95%", "Stop Codex at: off", "Stop Bedrock at: off"} <= set(desktop.icon.menu.texts())
    claude = desktop.icon.menu.item("Stop Claude at: 95%").action
    assert claude.texts() == ["Off", "95%"]  # Claude is at 92%
    assert [i.checked(i) for i in claude.items] == [False, True]
    assert all(i.radio for i in claude.items)
    desktop.icon.menu.item("Stop Codex at: off").action.item("85%").action()
    claude.item("Off").action()
    assert w.load_config() == {"cutoff": {"Codex": 85}}
    assert "Stop Codex at: 85%" in desktop.icon.menu.texts()  # menu rebuilt


def test_desktop_tray_credit_cutoff_menu(desktop):
    desktop._apply(sample_data())
    assert not any(" at extra usage" in t or " at credits left" in t for t in desktop.icon.menu.texts())
    data = sample_data()
    data[0].credits = w.Credits(used=3, unit="$", what="extra usage")  # no cap
    data[1].credits = w.Credits(left=42, what="credits left")
    desktop._apply(data)
    texts = desktop.icon.menu.texts()
    assert texts.index("Stop Claude at: off") + 1 == texts.index("Stop Claude at extra usage: off")
    assert texts.index("Stop Codex at: off") + 1 == texts.index("Stop Codex at credits left: off")
    claude = desktop.icon.menu.item("Stop Claude at extra usage: off").action
    assert claude.texts() == ["Off", "$5", "$7.50", "$10", "$15", "$20", "$30", "$50", "$75", "$100"]
    assert all(i.radio for i in claude.items)
    claude.item("$7.50").action()
    desktop.icon.menu.item("Stop Codex at credits left: off").action.item("10").action()
    assert w.load_config() == {"credit_cutoff": {"Claude": 7.5, "Codex": 10}}
    texts = desktop.icon.menu.texts()  # menu rebuilt
    assert "Stop Claude at extra usage: $7.50" in texts and "Stop Codex at credits left: 10" in texts
    claude = desktop.icon.menu.item("Stop Claude at extra usage: $7.50").action
    assert [i.checked(i) for i in claude.items] == [False, False, True] + [False] * 7
    assert [i.checked(i) for i in desktop.icon.menu.item("Stop Claude at: off").action.items][0]  # percent: off


def test_desktop_tray_errors_and_stale(desktop):
    desktop._apply(sample_data())
    desktop._apply([w.ProviderUsage("Claude", error="network: down"), w.ProviderUsage("Codex", error="HTTP 401")])
    texts = desktop.icon.menu.texts()
    assert "⚠ stale — network: down" in texts
    assert "⚠ stale — HTTP 401" in texts
    desktop.data = []
    desktop._apply([w.ProviderUsage("Claude", error="not logged in")])
    assert "⚠ not logged in" in desktop.icon.menu.texts()
    assert desktop.icon.title == "Claude –"


def test_desktop_tray_refresh_and_quit(desktop):
    desktop.icon.menu.item("Refresh now").action()
    assert desktop.wake.is_set()
    desktop.icon.menu.item("Quit").action()
    assert desktop.icon.stopped


def test_desktop_tray_loop_iteration(desktop, monkeypatch):
    class Stop(Exception):
        pass

    monkeypatch.setattr(w, "fetch_all", lambda: sample_data())
    monkeypatch.setattr(desktop.wake, "wait", lambda timeout: (_ for _ in ()).throw(Stop))
    icon = types.SimpleNamespace(visible=False)
    with pytest.raises(Stop):
        desktop._loop(icon)
    assert icon.visible
    assert desktop.last_fetch > 0
    assert desktop.icon.title.startswith("Claude 92%")


def test_desktop_tray_update_available(newer_release, desktop, monkeypatch):
    class Stop(Exception):
        pass

    opened = []
    monkeypatch.setattr(w.webbrowser, "open", opened.append)
    monkeypatch.setattr(w, "fetch_all", lambda: sample_data())
    monkeypatch.setattr(desktop.wake, "wait", lambda timeout: (_ for _ in ()).throw(Stop))
    with pytest.raises(Stop):
        desktop._loop(types.SimpleNamespace(visible=False))  # one fetch, with the update check
    texts = desktop.icon.menu.texts()
    assert texts[:3] == ["Update available: v99.0.0", "---", "Claude  (Max)"]
    assert desktop.icon.title == "Claude 92%  Codex 80%  Bedrock $5.00\nUpdate available: v99.0.0"
    desktop.icon.menu.item("Update available: v99.0.0").action()
    assert opened == [newer_release["url"]]
    desktop._apply(sample_data())  # no newer release (any more)
    assert "Update available: v99.0.0" not in desktop.icon.menu.texts()
    assert desktop.icon.title == "Claude 92%  Codex 80%  Bedrock $5.00"


def test_desktop_tray_update_notification_on_windows(newer_release, desktop, monkeypatch):
    class Stop(Exception):
        pass

    shown = []
    desktop.icon.HAS_NOTIFICATION = True
    desktop.icon.notify = lambda message, title: shown.append((title, message))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(w, "fetch_all", lambda: sample_data())
    monkeypatch.setattr(desktop.wake, "wait", lambda timeout: (_ for _ in ()).throw(Stop))
    for _ in range(2):
        desktop.wake.set()  # a second fetch: the notification is not repeated
        with pytest.raises(Stop):
            desktop._loop(types.SimpleNamespace(visible=False))
    assert shown == [("LLM Usage Widget", "Version 99.0.0 is available. Open the menu to get it.")]
    assert desktop._menu().texts()[0] == "Update available: v99.0.0"  # the menu built when it is opened


def test_desktop_tray_toggle_taskbar_text_keeps_other_settings(desktop):
    w.save_config({"bedrock": {"budget": "b"}})
    desktop._toggle_taskbar_text()
    assert w.load_config() == {"bedrock": {"budget": "b"}, "taskbar_text": False}
    desktop._toggle_taskbar_text()
    assert w.load_config()["taskbar_text"] is True


def test_desktop_tray_collapse_to_logo(desktop):
    desktop._apply(sample_data())
    gauge = desktop.icon.icon.tobytes()
    item = desktop.icon.menu.item(w.COLLAPSE_LABEL)
    assert not item.checked(item)
    item.action()
    assert w.load_config() == {"collapsed": True}
    assert desktop.icon.icon.tobytes() == w.logo_image().tobytes()
    assert desktop.icon.title == "Claude 92%  Codex 80%  Bedrock $5.00"  # the tooltip still has the numbers
    item = desktop.icon.menu.item(w.COLLAPSE_LABEL)
    assert item.checked(item)
    item.action()
    assert desktop.icon.icon.tobytes() == gauge


def test_desktop_tray_windows_menu_on_any_click(pystray, monkeypatch):
    pytest.importorskip("PIL")
    posted = []
    win32 = types.SimpleNamespace(WM_NOTIFY=0x4E, WM_LBUTTONUP=0x202, WM_RBUTTONUP=0x205,
                                  PostMessage=lambda *a: posted.append(a))
    util = types.ModuleType("pystray._util")
    util.win32 = win32
    monkeypatch.setitem(sys.modules, "pystray._util", util)
    monkeypatch.setattr(sys, "platform", "win32")
    seen = []
    monkeypatch.setattr(FakeIcon, "default_handlers", {0x4E: lambda wp, lp: seen.append(lp) or "handled"})

    class FakeStrip:
        def __init__(self, tray):
            self.closed = False
            self.focus_checks = 0

        def close(self):
            self.closed = True

        def take_focus_from_taskbar(self):
            self.focus_checks += 1
            return False

    monkeypatch.setattr(w, "TaskbarStrip", FakeStrip)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    tray = w.DesktopTray(60)
    assert tray.strip is not None
    tray.data = sample_data()
    handler = tray.icon._message_handlers[0x4E]
    assert handler(0, 0x202) == "handled"
    assert seen == [0x205]  # a left click opens the menu like a right click
    assert tray.icon.menu.texts()[0] == "Claude  (Max)"  # rebuilt on open
    assert tray.strip.focus_checks > 0  # once the menu has closed, the taskbar must not keep the focus
    assert handler(0, 0x200) == "handled"
    assert seen[-1] == 0x200  # other messages pass through
    assert tray.strip.focus_checks == 10  # not for other messages
    tray.icon._hwnd = 99
    tray.open_menu()
    assert posted == [(99, 0x4E, 0, 0x202)]
    texts = tray._menu().texts()
    assert "Show usage on the taskbar" in texts
    tray._quit()
    assert tray.strip.closed and not tray.icon.stopped  # the strip's loop stops the icon


def test_desktop_tray_survives_missing_tkinter(pystray, monkeypatch):
    pytest.importorskip("PIL")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(w.DesktopTray, "_open_menu_on_any_click", lambda self: None)
    warnings = []
    monkeypatch.setattr(w, "warn", warnings.append)

    def no_tk(tray):
        raise ImportError("No module named 'tkinter'")

    monkeypatch.setattr(w, "TaskbarStrip", no_tk)
    tray = w.DesktopTray(60)
    assert tray.strip is None
    assert "Taskbar text unavailable" in warnings[0]


# ==============================================================================
# The tray icon image
# ==============================================================================

def rgb(color: str):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def test_gauge_image_empty():
    pytest.importorskip("PIL")
    img = w.gauge_image([])
    assert img.mode == "RGBA" and img.size == (64, 64)
    assert img.getpixel((0, 0))[3] == 0  # transparent corner
    # Two tracks (Claude, Codex) with nothing filled.
    assert img.getpixel((16, 32))[:3] == rgb(w.THEME["track"])
    assert img.getpixel((48, 32))[:3] == rgb(w.THEME["track"])


def test_gauge_image_fill_and_colors():
    pytest.importorskip("PIL")
    data = [
        w.ProviderUsage("Claude", limits=[w.Limit("s", 50)]),
        w.ProviderUsage("Codex", limits=[w.Limit("s", 95)]),
        w.ProviderUsage("Bedrock", summary="$1", limits=[w.Limit("spend", None)]),
    ]
    img = w.gauge_image(data, size=100)
    gap = 10
    width = (100 - gap * 4) // 3
    centers = [gap + i * (width + gap) + width // 2 for i in range(3)]
    # Claude: half full, green below the middle, track above.
    assert img.getpixel((centers[0], 80))[:3] == rgb(w.THEME["normal"])
    assert img.getpixel((centers[0], 20))[:3] == rgb(w.THEME["track"])
    # Codex: critical, nearly full.
    assert img.getpixel((centers[1], 20))[:3] == rgb(w.THEME["critical"])
    # Bedrock: no percentage, so an empty track.
    assert img.getpixel((centers[2], 80))[:3] == rgb(w.THEME["track"])


def test_gauge_image_fills_to_credits_spent_once_the_plan_is_full():
    pytest.importorskip("PIL")
    data = [
        w.ProviderUsage("Claude", limits=[w.Limit("s", 100)], credits=w.Credits(used=12.5, cap=50, unit="$")),
        w.ProviderUsage("Codex", limits=[w.Limit("s", 100)], credits=w.Credits(left=42)),
        w.ProviderUsage("Vertex", limits=[w.Limit("s", 100)]),
    ]
    img = w.gauge_image(data, size=100)
    gap = 10
    width = (100 - gap * 4) // 3
    centers = [gap + i * (width + gap) + width // 2 for i in range(3)]
    # Claude: a quarter of the credits spent, green at the bottom, track above.
    assert img.getpixel((centers[0], 85))[:3] == rgb(w.THEME["normal"])
    assert img.getpixel((centers[0], 50))[:3] == rgb(w.THEME["track"])
    # Codex: a balance of unknown size, so the full plan bar, but no longer red.
    assert img.getpixel((centers[1], 20))[:3] == rgb(w.THEME["normal"])
    # Vertex: full and without credits, so red.
    assert img.getpixel((centers[2], 20))[:3] == rgb(w.THEME["critical"])


def test_gauge_image_caps_overfull():
    pytest.importorskip("PIL")
    img = w.gauge_image([w.ProviderUsage("Claude", limits=[w.Limit("s", 250)])], size=40)
    assert img.getpixel((0, 0))[3] == 0  # nothing drawn outside the gauge
