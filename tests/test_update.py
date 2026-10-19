"""The update check: version comparison, reading GitHub's latest release, caching and failures."""

from __future__ import annotations

import json
import urllib.error

import pytest

import usage_widget as w


def release(tag="v9.0.0", **extra):
    return {"tag_name": tag, "html_url": f"{w.REPO_URL}/releases/tag/{tag}", "draft": False,
            "prerelease": False, **extra}


# -- versions --------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("1.2.3", (1, 2, 3)), ("v1.2.3", (1, 2, 3)), (" v0.0.10 ", (0, 0, 10)), ("2", (2,)), ("v1.10", (1, 10)),
    ("1.2.3-rc1", None), ("vv1.2", None), ("1..2", None), ("latest", None), ("", None), (None, None),
])
def test_parse_version(text, expected):
    assert w.parse_version(text) == expected


@pytest.mark.parametrize("version, than, newer", [
    ("0.0.10", "0.0.9", True),  # numeric, not "0.0.10" < "0.0.9" as strings
    ("1.0.0", "0.9.9", True),
    ("v0.1", "0.0.5", True),
    ("1.2", "1.2.0", False),  # missing parts count as 0
    ("1.2.0.1", "1.2", True),
    ("0.0.5", "0.0.5", False),
    ("0.0.4", "0.0.5", False),
    ("garbage", "0.0.5", False),
    ("1.0.0", "dev", False),
])
def test_is_newer(version, than, newer):
    assert w.is_newer(version, than) is newer


def test_is_newer_defaults_to_this_copy(monkeypatch):
    monkeypatch.setattr(w, "__version__", "1.4.2")
    assert w.is_newer("1.4.3")
    assert not w.is_newer("1.4.2")


# -- reading the latest release ----------------------------------------------------

def test_latest_release(fake_http):
    fake_http.responses[w.RELEASES_API] = release("v1.2.3")
    assert w._latest_release() == {"version": "1.2.3", "url": f"{w.REPO_URL}/releases/tag/v1.2.3"}
    assert fake_http.kwargs == [{}] and fake_http.calls[0][1]["Accept"] == "application/vnd.github+json"


@pytest.mark.parametrize("reply", [
    release(draft=True), release(prerelease=True), release("nightly"), release("v2.0.0-beta"), release(""),
    {"message": "Not Found"}, [], "garbage", None,
])
def test_latest_release_unusable(fake_http, reply):
    fake_http.responses[w.RELEASES_API] = lambda url, kwargs: reply  # as is, even a list
    assert w._latest_release() is None


def test_latest_release_only_links_to_this_repo(fake_http):
    fake_http.responses[w.RELEASES_API] = release("v3.0.0", html_url="https://evil.example/download")
    assert w._latest_release()["url"] == f"{w.REPO_URL}/releases/tag/v3.0.0"
    fake_http.responses[w.RELEASES_API] = release("3.0.0", html_url=None)
    assert w._latest_release() == {"version": "3.0.0", "url": f"{w.REPO_URL}/releases/tag/3.0.0"}


# -- check_for_update --------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1_800_000_000.0}
    monkeypatch.setattr(w.time, "time", lambda: now["t"])
    return now


def test_check_for_update_newer(fake_http, monkeypatch):
    monkeypatch.setattr(w, "__version__", "0.0.5")
    fake_http.responses[w.RELEASES_API] = release("v0.0.10")
    assert w.check_for_update() == {"version": "0.0.10", "url": f"{w.REPO_URL}/releases/tag/v0.0.10"}
    assert w.update_label(w.check_for_update()) == "Update available: v0.0.10"


@pytest.mark.parametrize("tag", ["v0.0.5", "v0.0.4"])
def test_check_for_update_up_to_date(fake_http, monkeypatch, tag):
    monkeypatch.setattr(w, "__version__", "0.0.5")
    fake_http.responses[w.RELEASES_API] = release(tag)
    assert w.check_for_update() is None


def test_check_for_update_once_a_day(fake_http, clock, monkeypatch):
    monkeypatch.setattr(w, "__version__", "1.0.0")
    fake_http.responses[w.RELEASES_API] = [release("v1.1.0"), release("v1.2.0")]
    assert w.check_for_update()["version"] == "1.1.0"
    clock["t"] += w.UPDATE_INTERVAL - 60
    assert w.check_for_update()["version"] == "1.1.0"  # cached
    assert len(fake_http.calls) == 1
    clock["t"] += 120
    assert w.check_for_update()["version"] == "1.2.0"
    assert len(fake_http.calls) == 2


def test_check_for_update_cache_survives_restart(fake_http, monkeypatch):
    monkeypatch.setattr(w, "__version__", "1.0.0")
    fake_http.responses[w.RELEASES_API] = release("v1.1.0")
    w.check_for_update()
    cache = json.loads((w.config_dir() / "cloud-cache.json").read_text())
    assert cache["update:latest"]["value"]["version"] == "1.1.0"
    fake_http.responses.clear()  # a new process: nothing would answer
    assert w.check_for_update()["version"] == "1.1.0"
    # After updating, the same cached release is no longer news.
    monkeypatch.setattr(w, "__version__", "1.1.0")
    assert w.check_for_update() is None


def test_check_for_update_remembers_no_release(fake_http):
    fake_http.responses[w.RELEASES_API] = release(prerelease=True)
    assert w.check_for_update() is None
    assert w.check_for_update() is None
    assert len(fake_http.calls) == 1  # "nothing to offer" is cached for the day too


@pytest.mark.parametrize("error", [
    RuntimeError("network: [Errno 8] nodename nor servname provided"),
    w.AuthError("HTTP 403: login rejected"),  # GitHub's rate limit
    json.JSONDecodeError("Expecting value", "<html>", 0),
    urllib.error.URLError("timed out"),
    AssertionError("no network"),
])
def test_check_for_update_failures_are_silent(fake_http, clock, error):
    fake_http.responses[w.RELEASES_API] = error
    assert w.check_for_update() is None
    assert not (w.config_dir() / "cloud-cache.json").exists()  # a failure isn't cached as an answer
    clock["t"] += w.UPDATE_RETRY - 60
    assert w.check_for_update() is None
    assert len(fake_http.calls) == 1  # backing off
    clock["t"] += 120
    fake_http.responses[w.RELEASES_API] = release("v99.0.0")
    assert w.check_for_update()["version"] == "99.0.0"


def test_check_for_update_garbage_in_cache(fake_http, clock):
    path = w.config_dir() / "cloud-cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"update:latest": {"at": clock["t"], "value": "nonsense"}}))
    assert w.check_for_update() is None
    path.write_text(json.dumps({"update:latest": {"at": clock["t"], "value": {"version": "99.0"}}}))
    assert w.check_for_update() is None  # no URL to open
    assert fake_http.calls == []


@pytest.mark.parametrize("value", [False, "off", 0, None])
def test_check_for_update_disabled(fake_http, value):
    w.save_config({"update_check": value})
    fake_http.responses[w.RELEASES_API] = release("v99.0.0")
    assert w.check_for_update() is None
    assert fake_http.calls == []


def test_check_for_update_enabled_explicitly(fake_http):
    w.save_config({"update_check": True})
    fake_http.responses[w.RELEASES_API] = release("v99.0.0")
    assert w.check_for_update()["version"] == "99.0.0"


def test_real_http_failure_is_silent():
    # No fake_http: the conftest's blocked urlopen raises, as a broken network would.
    assert w.check_for_update() is None


# -- the desktop notification ------------------------------------------------------

UPDATE = {"version": "99.0.0", "url": f"{w.REPO_URL}/releases/tag/v99.0.0"}


@pytest.fixture
def runs(monkeypatch):
    calls = []
    monkeypatch.setattr(w.subprocess, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(w.shutil, "which", lambda name, **kw: f"/usr/bin/{name}")
    return calls


def test_notify_update_linux_once_per_version(runs, monkeypatch):
    monkeypatch.setattr(w.sys, "platform", "linux")
    w.notify_update(UPDATE)
    w.notify_update(UPDATE)  # already told about this one, also after a restart
    assert runs == [["notify-send", "--app-name", "LLM Usage Widget", "LLM Usage Widget",
                     "Version 99.0.0 is available. Open the menu to get it."]]
    w.notify_update({**UPDATE, "version": "99.1.0"})
    assert len(runs) == 2 and "99.1.0" in runs[1][-1]


def test_notify_update_macos(runs, monkeypatch):
    monkeypatch.setattr(w.sys, "platform", "darwin")
    w.notify_update(UPDATE)
    (args,) = runs
    assert args[0] == "osascript"
    assert args[-2:] == ["Version 99.0.0 is available. Open the menu to get it.", "LLM Usage Widget"]


def test_notify_update_with_the_tray_icon(runs):
    shown = []
    w.notify_update(UPDATE, lambda message, title: shown.append((title, message)))
    assert shown == [("LLM Usage Widget", "Version 99.0.0 is available. Open the menu to get it.")]
    assert runs == []


def test_notify_update_nothing_to_show_it_with(runs, monkeypatch):
    monkeypatch.setattr(w.sys, "platform", "linux")
    monkeypatch.setattr(w.shutil, "which", lambda name, **kw: None)  # no notify-send
    w.notify_update(UPDATE)
    monkeypatch.setattr(w.sys, "platform", "win32")  # the floating widget on Windows
    w.notify_update(UPDATE)
    assert runs == []
    assert not (w.config_dir() / "update-notified").exists()  # still to be shown, e.g. by the tray


def test_notify_update_failure_is_silent_and_retried(runs, monkeypatch):
    def broken(message, title):
        raise OSError("no notification service")

    w.notify_update(UPDATE, broken)
    shown = []
    w.notify_update(UPDATE, lambda message, title: shown.append(message))
    assert len(shown) == 1


def test_notify_update_nothing_new(runs):
    w.notify_update(None)
    assert runs == []
