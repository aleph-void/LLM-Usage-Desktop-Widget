"""Pure helpers: time parsing and formatting, labels, config files, HTTP."""

from __future__ import annotations

import io
import json
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

import usage_widget as w

UTC = timezone.utc


# -- Limit.level ---------------------------------------------------------------

@pytest.mark.parametrize("percent, expected", [
    (0, "normal"), (69.9, "normal"), (70, "warning"), (89.9, "warning"), (90, "critical"), (150, "critical"),
    (None, "normal"),
])
def test_level_from_percent(percent, expected):
    assert w.Limit("x", percent).level() == expected


@pytest.mark.parametrize("severity", ["warning", "critical"])
def test_level_explicit_severity_wins(severity):
    assert w.Limit("x", 1, severity=severity).level() == severity
    assert w.Limit("x", None, severity=severity).level() == severity


def test_level_ignores_unknown_severity():
    assert w.Limit("x", 95, severity="normal").level() == "critical"
    assert w.Limit("x", 10, severity="bogus").level() == "normal"


# -- parse_time ----------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, None),
    (0, datetime(1970, 1, 1, tzinfo=UTC)),
    (1_700_000_000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
    (1_700_000_000.5, datetime(2023, 11, 14, 22, 13, 20, 500000, tzinfo=UTC)),
    ("2025-01-02T03:04:05Z", datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)),
    ("2025-01-02T03:04:05+00:00", datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)),
    ("2025-01-02T05:04:05+02:00", datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)),
    ("not a date", None),
    ("", None),
    (1e20, None),  # out of range for a timestamp
])
def test_parse_time(value, expected):
    assert w.parse_time(value) == expected


# -- format_remaining ----------------------------------------------------------

def test_format_remaining_none():
    assert w.format_remaining(None) == ""


@pytest.mark.parametrize("delta, expected", [
    (timedelta(seconds=-5), "resetting"),
    (timedelta(0), "resetting"),
    (timedelta(seconds=30), "0m"),
    (timedelta(minutes=7, seconds=30), "7m"),
    (timedelta(hours=1, minutes=5, seconds=30), "1h 05m"),
    (timedelta(hours=23, minutes=59, seconds=30), "23h 59m"),
    (timedelta(days=1, hours=2, minutes=30), "1d 2h"),
    (timedelta(days=6, hours=23, minutes=30), "6d 23h"),
])
def test_format_remaining(frozen_now, delta, expected):
    now = frozen_now(datetime(2025, 6, 1, 12, 0, tzinfo=UTC))
    assert w.format_remaining(now + delta) == expected


# -- window_label --------------------------------------------------------------

@pytest.mark.parametrize("seconds, expected", [
    (None, "Limit"), (0, "Limit"), (604800, "Weekly"), ("604800", "Weekly"), (18000, "5h"), (3600, "1h"),
    (86400, "1d"), (2592000, "30d"), (900, "15m"), (5400, "90m"),
])
def test_window_label(seconds, expected):
    assert w.window_label(seconds) == expected


# -- small formatters ----------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, False), ("", False), ("0", False), ("false", False), ("FALSE", False), (" no ", False), ("off", False),
    (0, False), ("1", True), ("true", True), ("yes", True), (1, True), ("anything", True),
])
def test_truthy(value, expected):
    assert w._truthy(value) is expected


@pytest.mark.parametrize("amount, unit, expected", [
    (0, "USD", "$0.00"), (12.345, "USD", "$12.35"), (1234567.8, "", "$1,234,567.80"), (5, "EUR", "5.00 EUR"),
])
def test_money(amount, unit, expected):
    assert w._money(amount, unit) == expected


@pytest.mark.parametrize("n, expected", [
    (0, "0"), (999, "999"), (1000, "1.0k"), (1500, "1.5k"), (2_500_000, "2.5M"), (3_000_000_000, "3.0B"),
])
def test_human(n, expected):
    assert w._human(n) == expected


# -- _next_period --------------------------------------------------------------

@pytest.mark.parametrize("now, unit, expected", [
    (datetime(2025, 3, 15, 10, tzinfo=UTC), "DAILY", datetime(2025, 3, 16, tzinfo=UTC)),
    (datetime(2025, 12, 31, 23, tzinfo=UTC), "DAILY", datetime(2026, 1, 1, tzinfo=UTC)),
    (datetime(2025, 3, 15, tzinfo=UTC), "MONTHLY", datetime(2025, 4, 1, tzinfo=UTC)),
    (datetime(2025, 1, 31, tzinfo=UTC), "MONTHLY", datetime(2025, 2, 1, tzinfo=UTC)),
    (datetime(2025, 11, 30, tzinfo=UTC), "MONTHLY", datetime(2025, 12, 1, tzinfo=UTC)),
    (datetime(2025, 12, 31, tzinfo=UTC), "MONTHLY", datetime(2026, 1, 1, tzinfo=UTC)),
    (datetime(2025, 1, 1, tzinfo=UTC), "QUARTERLY", datetime(2025, 4, 1, tzinfo=UTC)),
    (datetime(2025, 3, 31, tzinfo=UTC), "QUARTERLY", datetime(2025, 4, 1, tzinfo=UTC)),
    (datetime(2025, 8, 31, tzinfo=UTC), "QUARTERLY", datetime(2025, 10, 1, tzinfo=UTC)),
    (datetime(2025, 10, 1, tzinfo=UTC), "QUARTERLY", datetime(2026, 1, 1, tzinfo=UTC)),
    (datetime(2025, 12, 31, tzinfo=UTC), "QUARTERLY", datetime(2026, 1, 1, tzinfo=UTC)),
    (datetime(2025, 6, 30, tzinfo=UTC), "ANNUALLY", datetime(2026, 1, 1, tzinfo=UTC)),
    (datetime(2025, 6, 30, tzinfo=UTC), "CUSTOM", None),
    (datetime(2025, 6, 30, tzinfo=UTC), None, None),
])
def test_next_period(frozen_now, now, unit, expected):
    frozen_now(now)
    assert w._next_period(unit) == expected


# -- config --------------------------------------------------------------------

def test_config_dir_linux_uses_xdg(monkeypatch, home):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    assert w.config_dir() == home / "xdg" / "llm-usage-widget"


def test_config_dir_linux_default(monkeypatch, home):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert w.config_dir() == home / ".config" / "llm-usage-widget"


def test_config_dir_macos(monkeypatch, home):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert w.config_dir() == home / "Library" / "Application Support" / "llm-usage-widget"


def test_config_dir_windows(monkeypatch, home):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(home / "Roaming"))
    assert w.config_dir() == home / "Roaming" / "llm-usage-widget"
    monkeypatch.delenv("APPDATA")
    assert w.config_dir() == home / "AppData" / "Roaming" / "llm-usage-widget"


def test_load_config_missing():
    assert w.load_config() == {}


def test_save_then_load_config():
    w.save_config({"x": 10, "topmost": False, "bedrock": {"regions": ["us-west-2"]}})
    assert w.load_config() == {"x": 10, "topmost": False, "bedrock": {"regions": ["us-west-2"]}}


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"string"', "null"])
def test_load_config_rejects_garbage(text):
    path = w.config_dir() / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    assert w.load_config() == {}


def test_save_config_swallows_errors(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(w, "config_dir", lambda: blocker / "sub")  # parent is a file: mkdir fails
    w.save_config({"a": 1})  # must not raise


# -- top_limit / summary_text / usage_pages --------------------------------------

def test_top_limit_picks_fullest_bar():
    u = w.ProviderUsage("Claude", limits=[w.Limit("a", 10), w.Limit("spend", None), w.Limit("b", 80),
                                          w.Limit("c", 30)])
    assert w.top_limit(u).label == "b"


def test_top_limit_none_without_bars():
    assert w.top_limit(w.ProviderUsage("Bedrock", limits=[w.Limit("spend", None)])) is None
    assert w.top_limit(w.ProviderUsage("Claude")) is None


def test_summary_text():
    data = [
        w.ProviderUsage("Claude", limits=[w.Limit("s", 98.6), w.Limit("w", 40)]),
        w.ProviderUsage("Codex", limits=[w.Limit("s", 13.2)]),
        w.ProviderUsage("Bedrock", summary="$12.34", limits=[w.Limit("This month", None)]),
        w.ProviderUsage("Broken", error="HTTP 500"),
        w.ProviderUsage("Silent"),
    ]
    assert w.summary_text(data) == "Claude 99%  Codex 13%  Bedrock $12.34  Broken –"
    assert w.summary_text([]) == ""


def test_usage_pages():
    assert w.usage_pages([]) == {"Claude": w.USAGE_PAGES["Claude"], "Codex": w.USAGE_PAGES["Codex"]}
    assert list(w.usage_pages([w.ProviderUsage("Codex"), w.ProviderUsage("Bedrock")])) == ["Codex", "Bedrock"]


# -- http_json -----------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_http_json_success(monkeypatch):
    seen = {}

    def urlopen(req, timeout, context):
        seen.update(url=req.full_url, headers=dict(req.header_items()), timeout=timeout, context=context)
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert w.http_json("https://example.test/x", {"Authorization": "Bearer t"}, timeout=5) == {"ok": True}
    assert seen["url"] == "https://example.test/x"
    assert seen["timeout"] == 5
    assert seen["headers"]["Authorization"] == "Bearer t"
    assert seen["headers"]["Accept"] == "application/json"
    assert seen["headers"]["User-agent"] == f"llm-usage-widget/{w.__version__}"
    assert isinstance(seen["context"], ssl.SSLContext)
    assert w._SSL_CONTEXT is seen["context"]  # created once and reused


def test_http_json_caller_headers_override_defaults(monkeypatch):
    seen = {}

    def urlopen(req, timeout, context):
        seen.update(dict(req.header_items()))
        return FakeResponse(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    w.http_json("https://example.test", {"Accept": "text/plain"})
    assert seen["Accept"] == "text/plain"


@pytest.mark.parametrize("code", [401, 403])
def test_http_json_auth_errors(monkeypatch, code):
    def urlopen(req, timeout, context):
        raise urllib.error.HTTPError(req.full_url, code, "nope", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(w.AuthError, match=f"HTTP {code}"):
        w.http_json("https://example.test", {})


def test_http_json_other_http_error(monkeypatch):
    def urlopen(req, timeout, context):
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="^HTTP 503$"):
        w.http_json("https://example.test", {})


def test_http_json_network_error(monkeypatch):
    def urlopen(req, timeout, context):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="network: name resolution failed"):
        w.http_json("https://example.test", {})


def test_ssl_context_without_truststore(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)  # import fails
    ctx = w._ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname


def test_ssl_context_prefers_truststore(monkeypatch):
    import types

    made = []
    fake = types.SimpleNamespace(SSLContext=lambda proto: made.append(proto) or "truststore-ctx")
    monkeypatch.setitem(sys.modules, "truststore", fake)
    assert w._ssl_context() == "truststore-ctx"
    assert made == [ssl.PROTOCOL_TLS_CLIENT]


# -- http_json: POST bodies and cloud-style errors -------------------------------

def test_http_json_posts_json(monkeypatch):
    seen = {}

    def urlopen(req, timeout, context):
        seen.update(method=req.get_method(), data=req.data, headers=dict(req.header_items()))
        return FakeResponse(b'{"ok": 1}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert w.http_json("https://example.test", {}, body={"q": "x"}) == {"ok": 1}
    assert seen["method"] == "POST"
    assert json.loads(seen["data"]) == {"q": "x"}
    assert seen["headers"]["Content-type"] == "application/json"


def test_http_json_posts_form(monkeypatch):
    seen = {}

    def urlopen(req, timeout, context):
        seen.update(method=req.get_method(), data=req.data, headers=dict(req.header_items()))
        return FakeResponse(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    w.http_json("https://example.test", {}, form={"grant_type": "refresh_token", "a": "b c"})
    assert seen["method"] == "POST"
    assert seen["data"] == b"grant_type=refresh_token&a=b+c"
    assert seen["headers"]["Content-type"] == "application/x-www-form-urlencoded"


def http_error(code, body: bytes):
    def urlopen(req, timeout, context):
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(body))

    return urlopen


@pytest.mark.parametrize("code", [401, 403, 404, 500])
def test_http_json_without_auth_errors_raises_status(monkeypatch, code):
    monkeypatch.setattr(urllib.request, "urlopen",
                        http_error(code, b'{"error": {"code": 7, "message": "Permission  denied\\n on x"}}'))
    with pytest.raises(w.HTTPStatusError) as info:
        w.http_json("https://example.test", {}, auth_errors=False)
    assert info.value.code == code
    assert str(info.value) == f"HTTP {code}: Permission denied on x"
    assert isinstance(info.value, RuntimeError)  # callers catching RuntimeError still see it


@pytest.mark.parametrize("body, detail", [
    (b'{"error": {"code": "AuthorizationFailed"}}', "AuthorizationFailed"),
    (b'{"error": "invalid_grant"}', "invalid_grant"),
    (b'{"error": null}', ""),
    (b'{"other": 1}', ""),
    (b"[1]", ""),
    (b"<html>oops</html>", ""),
    (b"", ""),
])
def test_error_detail(monkeypatch, body, detail):
    monkeypatch.setattr(urllib.request, "urlopen", http_error(400, body))
    with pytest.raises(w.HTTPStatusError) as info:
        w.http_json("https://example.test", {}, auth_errors=False)
    assert str(info.value) == "HTTP 400" + (f": {detail}" if detail else "")


def test_error_detail_is_shortened(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", http_error(400, json.dumps({"error": {"message": "x" * 200}}).encode()))
    with pytest.raises(w.HTTPStatusError) as info:
        w.http_json("https://example.test", {}, auth_errors=False)
    detail = str(info.value).split(": ", 1)[1]
    assert len(detail) == 90 and detail.endswith("…")


# -- find_cli / run_cli ----------------------------------------------------------

REAL_RUN_CLI = w.run_cli  # conftest stubs it out for every test


def make_tool(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path = directory / f"{name}.bat"
        path.write_text("@echo off\r\necho hi\r\n")
    else:
        path = directory / name
        path.write_text("#!/bin/sh\necho hi\n")
        path.chmod(0o755)
    return path


def test_find_cli_on_path(monkeypatch, tmp_path):
    tool = make_tool(tmp_path / "bin", "fakecli")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert w.find_cli("fakecli").lower() == str(tool).lower()


def test_find_cli_in_install_folders(monkeypatch, tmp_path, home):
    tool = make_tool(home / "google-cloud-sdk" / "bin", "fakegcloud")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert w.find_cli("fakegcloud").lower() == str(tool).lower()


def test_find_cli_windows_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    searched = []

    def which(name, path=None):
        searched.append(path)
        return None

    monkeypatch.setattr(w.shutil, "which", which)
    assert w.find_cli("az") is None
    assert str(tmp_path / "local" / "Microsoft SDKs" / "Azure" / "CLI2" / "wbin") in searched[1]


def test_find_cli_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert w.find_cli("definitely-not-installed-xyz") is None


def test_run_cli(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="token\n")

    monkeypatch.setattr(w, "find_cli", lambda name: f"/opt/{name}")
    monkeypatch.setattr(w.subprocess, "run", run)
    assert REAL_RUN_CLI(["gcloud", "auth", "print-access-token"]) == "token\n"
    cmd, kwargs = calls[0]
    assert cmd == ["/opt/gcloud", "auth", "print-access-token"]
    assert kwargs["capture_output"] and kwargs["timeout"] == 30


@pytest.mark.parametrize("outcome", [
    subprocess.CompletedProcess([], 1, stdout="partial"), FileNotFoundError("x"), subprocess.TimeoutExpired("x", 1)])
def test_run_cli_failures(monkeypatch, outcome):
    def run(cmd, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(w, "find_cli", lambda name: "/opt/x")
    monkeypatch.setattr(w.subprocess, "run", run)
    assert REAL_RUN_CLI(["x"]) is None


def test_run_cli_missing_tool(monkeypatch):
    monkeypatch.setattr(w, "find_cli", lambda name: None)
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: pytest.fail("ran a missing tool"))
    assert REAL_RUN_CLI(["gcloud"]) is None


def test_rfc3339():
    assert w._rfc3339(datetime(2025, 1, 2, 3, 4, 5, 999, tzinfo=timezone(timedelta(hours=2)))) == "2025-01-02T01:04:05Z"


def test_usage_pages_hide_unused_clouds():
    assert set(w.usage_pages([])) == {"Claude", "Codex"}
    assert set(w.usage_pages([w.ProviderUsage("Vertex"), w.ProviderUsage("Azure")])) == {"Vertex", "Azure"}
