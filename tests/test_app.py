"""fetch_all, the --once output, the single-instance lock and main()'s choice of front end."""

from __future__ import annotations

import os
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

import usage_widget as w
from conftest import ROOT

# -- fetch_all -----------------------------------------------------------------


def test_fetch_all_handles_each_outcome(monkeypatch):
    def ok():
        return w.ProviderUsage("A", limits=[w.Limit("x", 1)])

    def not_used():
        raise w.NotUsed

    def auth():
        raise w.AuthError("log in again")

    def crash():
        raise KeyError("boom")

    def silent_crash():
        raise ValueError

    monkeypatch.setattr(w, "PROVIDERS", (("A", ok), ("B", not_used), ("C", auth), ("D", crash),
                                         ("E", silent_crash)))
    monkeypatch.setattr(w.time, "time", lambda: 1234.0)
    results = w.fetch_all()
    assert [(u.name, u.error) for u in results] == [
        ("A", ""), ("C", "log in again"), ("D", "'boom'"), ("E", "ValueError")]
    assert all(u.fetched_at == 1234.0 for u in results)


def test_fetch_all_with_nothing_set_up():
    # A clean machine: Claude and Codex ask for a login, the clouds stay out.
    results = w.fetch_all()
    assert [u.name for u in results] == ["Claude", "Codex"]
    assert "not logged in" in results[0].error
    assert "codex login" in results[1].error


def test_providers_order():
    assert [name for name, _ in w.PROVIDERS] == ["Claude", "Codex", "Bedrock", "Vertex", "Azure"]


# -- print_once ----------------------------------------------------------------

def test_print_once(monkeypatch, capsys):
    soon = datetime.now(timezone.utc) + timedelta(hours=2, minutes=30, seconds=30)
    monkeypatch.setattr(w, "fetch_all", lambda: [
        w.ProviderUsage("Claude", plan="max", limits=[w.Limit("Session (5h)", 42, soon), w.Limit("Weekly", 100)]),
        w.ProviderUsage("Codex", error="HTTP 500"),
        w.ProviderUsage("Bedrock", limits=[w.Limit("This month $3.00", None, soon), w.Limit("Tokens", None)]),
    ])
    assert w.print_once() == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Claude (max)"
    assert lines[1].startswith("  Session (5h)")
    assert "  42%  [########            ]  resets 2h 30m" in lines[1]
    assert "100%  [####################]" in lines[2]
    assert lines[3:] == ["Codex", "  ! HTTP 500", "Bedrock", "  This month $3.00  (resets 2h 30m)", "  Tokens"]


# -- single instance -----------------------------------------------------------

def test_single_instance_lock():
    first = w.acquire_single_instance()
    assert first is not None
    try:
        assert w.acquire_single_instance() is None
    finally:
        first.close()
    if sys.platform != "win32":  # Windows releases a closed handle's lock only eventually
        again = w.acquire_single_instance()
        assert again is not None
        again.close()


# -- main() --------------------------------------------------------------------

class Recorder:
    """Stands in for a front-end class: records how it was built and whether it ran."""

    def __init__(self):
        self.made = []
        self.ran = 0

    def __call__(self, *args):
        self.made.append(args)
        return self

    def run(self):
        self.ran += 1


@pytest.fixture
def fronts(monkeypatch):
    r = types.SimpleNamespace(widget=Recorder(), tray=Recorder(), desktop=Recorder(), warnings=[])
    monkeypatch.setattr(w, "Widget", r.widget)
    monkeypatch.setattr(w, "Tray", r.tray)
    monkeypatch.setattr(w, "DesktopTray", r.desktop)
    monkeypatch.setattr(w, "warn", r.warnings.append)
    monkeypatch.setattr(w, "acquire_single_instance", lambda: object())
    return r


def run_main(monkeypatch, *args, platform="linux"):
    monkeypatch.setattr(sys, "argv", ["usage_widget.py", *args])
    monkeypatch.setattr(sys, "platform", platform)
    return w.main()


def test_main_version(monkeypatch, capsys):
    with pytest.raises(SystemExit) as info:
        run_main(monkeypatch, "--version")
    assert info.value.code == 0
    assert w.__version__ in capsys.readouterr().out


def test_main_rejects_tray_and_widget(monkeypatch, capsys):
    with pytest.raises(SystemExit) as info:
        run_main(monkeypatch, "--tray", "--widget")
    assert info.value.code == 2


def test_main_once(monkeypatch, fronts):
    monkeypatch.setattr(w, "print_once", lambda: 7)
    monkeypatch.setattr(w, "acquire_single_instance", lambda: pytest.fail("--once must not take the lock"))
    assert run_main(monkeypatch, "--once") == 7


def test_main_already_running(monkeypatch, fronts):
    monkeypatch.setattr(w, "acquire_single_instance", lambda: None)
    assert run_main(monkeypatch) == 0
    assert fronts.warnings == ["LLM Usage Widget is already running."]
    assert not (fronts.widget.ran or fronts.tray.ran or fronts.desktop.ran)


def test_main_linux_appindicator(monkeypatch, fronts):
    monkeypatch.setattr(w, "load_appindicator", lambda: "AI")
    assert run_main(monkeypatch, "--interval", "90") == 0
    assert fronts.tray.made == [(90, "AI")]
    assert fronts.tray.ran == 1


def test_main_linux_without_appindicator_falls_back(monkeypatch, fronts):
    monkeypatch.setattr(w, "load_appindicator", lambda: None)
    assert run_main(monkeypatch) == 0
    assert fronts.widget.made == [(w.DEFAULT_INTERVAL, False)]
    assert "Falling back to the floating widget" in fronts.warnings[0]


def test_main_linux_tray_required(monkeypatch, fronts):
    monkeypatch.setattr(w, "load_appindicator", lambda: None)
    assert run_main(monkeypatch, "--tray") == 1
    assert not fronts.widget.made
    assert "AppIndicator" in fronts.warnings[0] or "appindicator" in fronts.warnings[0]


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_main_widget_flag(monkeypatch, fronts, platform):
    monkeypatch.setattr(w, "load_appindicator", lambda: pytest.fail("not needed for --widget"))
    assert run_main(monkeypatch, "--widget", "--framed", "--interval", "5", platform=platform) == 0
    assert fronts.widget.made == [(5, True)]
    assert not fronts.desktop.made


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_main_desktop_tray(monkeypatch, fronts, platform):
    monkeypatch.setitem(sys.modules, "pystray", types.ModuleType("pystray"))
    monkeypatch.setitem(sys.modules, "PIL", types.ModuleType("PIL"))
    if platform == "win32":
        dpi = []
        fake_windll = types.SimpleNamespace(shcore=types.SimpleNamespace(SetProcessDpiAwareness=dpi.append))
        import ctypes
        monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    assert run_main(monkeypatch, platform=platform) == 0
    assert fronts.desktop.made == [(w.DEFAULT_INTERVAL,)]
    assert fronts.desktop.ran == 1
    if platform == "win32":
        assert dpi == [1]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_main_desktop_tray_missing_deps(monkeypatch, fronts, platform):
    monkeypatch.setitem(sys.modules, "pystray", None)
    assert run_main(monkeypatch, platform=platform) == 0
    assert fronts.widget.made
    assert "pystray" in fronts.warnings[0]
    fronts.widget.made.clear()
    assert run_main(monkeypatch, "--tray", platform=platform) == 1
    assert not fronts.widget.made


def test_warn_without_stderr(monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    w.warn("nobody hears this")  # pythonw has no stderr; must not raise


def test_warn(capsys):
    w.warn("hello")
    assert capsys.readouterr().err == "hello\n"


# -- the script itself ---------------------------------------------------------

def run_script(home, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "CLAUDE_", "CODEX_", "ANTHROPIC_"))}
    env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               APPDATA=str(home / "AppData"), CLAUDE_CONFIG_DIR=str(home / ".claude"),
               CODEX_HOME=str(home / ".codex"), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(ROOT / "usage_widget.py"), *args], capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=60, check=False)


def test_script_version(home):
    out = run_script(home, "--version")
    assert out.returncode == 0
    assert out.stdout.strip() == f"usage_widget.py {w.__version__}"


def test_script_help(home):
    out = run_script(home, "--help")
    assert out.returncode == 0
    for flag in ("--once", "--interval", "--tray", "--widget", "--framed"):
        assert flag in out.stdout


@pytest.mark.skipif(sys.platform == "darwin", reason="would query the real login Keychain")
def test_script_once_on_a_clean_machine(home):
    out = run_script(home, "--once")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        "Claude", "  ! not logged in — run `claude` and /login",
        "Codex", "  ! not logged in — run `codex login`",
    ]
