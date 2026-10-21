"""Claude: Claude Code's OAuth login, Bedrock detection and the usage response."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

import usage_widget as w

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def write_credentials(home, oauth: dict | None = None, raw: str | None = None):
    path = home / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    return path


def write_settings(home, env: dict | None = None, raw: str | None = None):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps({"env": env}), encoding="utf-8")


@pytest.fixture
def logged_in(home):
    oauth = {"accessToken": "tok-123", "subscriptionType": "max", "expiresAt": (time.time() + 3600) * 1000}
    write_credentials(home, oauth)
    return oauth


# -- credentials ---------------------------------------------------------------

def test_credentials_from_file(logged_in):
    assert w._claude_credentials()["accessToken"] == "tok-123"


def test_credentials_honour_claude_config_dir(monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "custom"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    assert w._claude_credentials()["accessToken"] == "custom"


def test_credentials_default_to_home_dot_claude(monkeypatch, home):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    write_credentials(home, {"accessToken": "from-home"})
    assert w._claude_credentials()["accessToken"] == "from-home"


def test_not_logged_in():
    with pytest.raises(w.AuthError, match="not logged in"):
        w._claude_credentials()


@pytest.mark.parametrize("raw", ["{broken", "[]", json.dumps({"claudeAiOauth": {}}),
                                 json.dumps({"claudeAiOauth": {"accessToken": ""}}), json.dumps({"other": 1})])
def test_unusable_credentials_file(home, raw):
    write_credentials(home, raw=raw)
    with pytest.raises(w.AuthError):
        w._claude_credentials()


def test_bedrock_user_without_login_is_left_out(home):
    write_settings(home, {"CLAUDE_CODE_USE_BEDROCK": "1"})
    with pytest.raises(w.NotUsed):
        w._claude_credentials()


def test_macos_keychain_is_preferred(monkeypatch, home):
    write_credentials(home, {"accessToken": "from-file"})
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"claudeAiOauth": {"accessToken": "kc"}}))

    monkeypatch.setattr(w.subprocess, "run", run)
    assert w._claude_credentials()["accessToken"] == "kc"
    assert calls == [["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]]


@pytest.mark.parametrize("outcome", [
    subprocess.CompletedProcess([], 44, stdout=""),
    subprocess.CompletedProcess([], 0, stdout="   "),
    subprocess.CompletedProcess([], 0, stdout="not json"),
    FileNotFoundError("security"),
    subprocess.TimeoutExpired("security", 10),
])
def test_macos_keychain_failure_falls_back_to_file(monkeypatch, home, outcome):
    write_credentials(home, {"accessToken": "from-file"})
    monkeypatch.setattr(sys, "platform", "darwin")

    def run(cmd, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(w.subprocess, "run", run)
    assert w._claude_credentials()["accessToken"] == "from-file"


def test_keychain_only_consulted_on_macos(monkeypatch, logged_in):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: pytest.fail("Keychain queried off macOS"))
    w._claude_credentials()


# -- Claude Code settings / Bedrock detection ----------------------------------

def test_claude_code_env_overlays_settings(monkeypatch, home):
    monkeypatch.setenv("FROM_PROCESS", "a")
    monkeypatch.setenv("OVERRIDDEN", "process")
    write_settings(home, {"OVERRIDDEN": "settings", "NUMBER": 1, "FLAG": True})
    env = w.claude_code_env()
    assert env["FROM_PROCESS"] == "a"
    assert env["OVERRIDDEN"] == "settings"
    assert env["NUMBER"] == "1"
    assert env["FLAG"] == "True"


@pytest.mark.parametrize("raw", ["{oops", "[]", json.dumps({"env": ["x"]}), json.dumps({"env": None}), "{}"])
def test_claude_code_env_tolerates_bad_settings(monkeypatch, home, raw):
    monkeypatch.setenv("KEEP", "yes")
    write_settings(home, raw=raw)
    assert w.claude_code_env()["KEEP"] == "yes"


@pytest.mark.parametrize("env, expected", [
    ({}, False),
    ({"CLAUDE_CODE_USE_BEDROCK": "1"}, True),
    ({"CLAUDE_CODE_USE_BEDROCK": "true"}, True),
    ({"CLAUDE_CODE_USE_BEDROCK": "0"}, False),
    ({"CLAUDE_CODE_USE_BEDROCK": "false"}, False),
    ({"ANTHROPIC_BASE_URL": "https://bedrock-runtime.us-east-1.amazonaws.com"}, True),
    ({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}, False),
])
def test_claude_uses_bedrock(env, expected):
    assert w.claude_uses_bedrock(env) is expected


def test_claude_uses_bedrock_reads_settings_by_default(home):
    assert w.claude_uses_bedrock() is False
    write_settings(home, {"CLAUDE_CODE_USE_BEDROCK": "1"})
    assert w.claude_uses_bedrock() is True


def test_claude_uses_bedrock_reads_process_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    assert w.claude_uses_bedrock() is True


# -- fetch_claude --------------------------------------------------------------

def test_expired_login(home, fake_http):
    write_credentials(home, {"accessToken": "t", "expiresAt": (time.time() - 60) * 1000})
    with pytest.raises(w.AuthError, match="expired"):
        w.fetch_claude()
    assert fake_http.calls == []


def test_request_headers(logged_in, fake_http):
    fake_http.responses[USAGE_URL] = {}
    w.fetch_claude()
    url, headers = fake_http.calls[0]
    assert url == USAGE_URL
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"


def test_usage_names_the_account(logged_in, home, fake_http):
    fake_http.responses[USAGE_URL] = {}
    assert w.fetch_claude().account == ""
    (home / ".claude" / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": "acc-1", "organizationUuid": "org-1"}}), encoding="utf-8")
    assert w.fetch_claude().account == "acc-1/org-1"


def test_account_read_from_home_without_claude_config_dir(monkeypatch, home):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acc-2"}}), encoding="utf-8")
    assert w.claude_account() == "acc-2"


def test_login_without_expiry_is_used(home, fake_http):
    write_credentials(home, {"accessToken": "t"})
    fake_http.responses[USAGE_URL] = {}
    usage = w.fetch_claude()
    assert usage.name == "Claude"
    assert usage.plan == ""
    assert usage.limits == []


def test_limits_list_shape(logged_in, fake_http):
    fake_http.responses[USAGE_URL] = {"limits": [
        {"kind": "session", "percent": 42.5, "resets_at": "2030-01-01T00:00:00Z"},
        {"kind": "weekly_all", "percent": 80, "resets_at": 1_900_000_000, "severity": "warning"},
        {"kind": "weekly_scoped", "percent": 10, "scope": {"model": {"display_name": "Opus"}}},
        {"kind": "weekly_scoped", "percent": 11, "scope": {"surface": "claude_code"}},
        {"kind": "weekly_scoped", "percent": None},
        {"kind": "some_new_kind", "percent": 5},
        {"kind": "other", "percent": 6, "scope": {"model": {"display_name": "Sonnet"}}},
    ]}
    usage = w.fetch_claude()
    assert usage.plan == "max"
    assert [(lim.label, lim.percent) for lim in usage.limits] == [
        ("Session (5h)", 42.5), ("Weekly", 80.0), ("Weekly · Opus", 10.0), ("Weekly · claude_code", 11.0),
        ("Weekly · scoped", 0.0), ("Some new kind", 5.0), ("Weekly · Sonnet", 6.0),
    ]
    assert usage.limits[0].resets_at == datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert usage.limits[1].resets_at == datetime.fromtimestamp(1_900_000_000, tz=timezone.utc)
    assert usage.limits[1].level() == "warning"
    assert usage.limits[2].resets_at is None


def test_legacy_shape(logged_in, fake_http):
    fake_http.responses[USAGE_URL] = {
        "five_hour": {"utilization": 12, "resets_at": "2030-01-01T05:00:00+00:00"},
        "seven_day": {"utilization": 55.5, "resets_at": None},
        "seven_day_opus": {"utilization": None},
        "seven_day_sonnet": {"utilization": 0},
        "limits": [],  # empty list falls through to the old keys
    }
    usage = w.fetch_claude()
    assert [(lim.label, lim.percent) for lim in usage.limits] == [
        ("Session (5h)", 12.0), ("Weekly", 55.5), ("Weekly · Sonnet", 0.0)]
    assert usage.limits[0].resets_at == datetime(2030, 1, 1, 5, tzinfo=timezone.utc)


def test_extra_usage_with_cap(logged_in, fake_http):
    fake_http.responses[USAGE_URL] = {
        "extra_usage": {"is_enabled": True, "used_credits": 1250, "monthly_limit": 5000, "utilization": 25}}
    usage = w.fetch_claude()
    (lim,) = usage.limits
    assert (lim.label, lim.percent) == ("Extra usage $12.50 / $50", 25.0)
    assert usage.credits == w.Credits(used=12.5, cap=50.0, unit="$", what="extra usage")


def test_extra_usage_without_cap(logged_in, fake_http):
    fake_http.responses[USAGE_URL] = {"extra_usage": {"is_enabled": True, "used_credits": 300}}
    usage = w.fetch_claude()
    (lim,) = usage.limits
    assert (lim.label, lim.percent) == ("Extra usage $3.00", 0.0)
    assert usage.credits == w.Credits(used=3.0, cap=None, unit="$", what="extra usage")


@pytest.mark.parametrize("extra", [None, {}, {"is_enabled": False, "used_credits": 5}, {"is_enabled": True}])
def test_extra_usage_hidden(logged_in, fake_http, extra):
    fake_http.responses[USAGE_URL] = {"extra_usage": extra}
    assert w.fetch_claude().limits == []


def test_extra_usage_enabled_but_unused_still_counts_as_credits(logged_in, fake_http):
    """Nothing spent yet, so no row, but a credit cutoff can already be set."""
    fake_http.responses[USAGE_URL] = {"extra_usage": {"is_enabled": True, "monthly_limit": 0}}
    usage = w.fetch_claude()
    assert usage.limits == []
    assert usage.credits == w.Credits(used=0.0, cap=None, unit="$", what="extra usage")


def test_extra_usage_amounts_are_cents(logged_in, fake_http):
    """The API reports extra usage in cents: 6155 is $61.55, not $6155."""
    fake_http.responses[USAGE_URL] = {"extra_usage": {"is_enabled": True, "used_credits": 6155}}
    usage = w.fetch_claude()
    assert usage.credits.used == 61.55
    assert w.credits_headline(usage.credits)[0] == "$61.55 used"


@pytest.mark.parametrize("extra", [None, {}, "garbage", {"is_enabled": False, "used_credits": 5},
                                   {"is_enabled": True, "used_credits": "lots"},
                                   {"is_enabled": True, "used_credits": float("nan")},
                                   {"is_enabled": True, "used_credits": -1}])
def test_no_credits_detected(logged_in, fake_http, extra):
    fake_http.responses[USAGE_URL] = {"extra_usage": extra}
    assert w.fetch_claude().credits is None


def test_http_auth_error_propagates(logged_in, fake_http):
    fake_http.responses[USAGE_URL] = w.AuthError("HTTP 401: login rejected")
    with pytest.raises(w.AuthError):
        w.fetch_claude()


@pytest.mark.parametrize("env", [{"CLAUDE_CODE_USE_VERTEX": "1"}, {"CLAUDE_CODE_USE_FOUNDRY": "true"}])
def test_cloud_user_without_login_is_left_out(home, env):
    write_settings(home, env)
    with pytest.raises(w.NotUsed):
        w._claude_credentials()


@pytest.mark.parametrize("fn, var", [(w.claude_uses_vertex, "CLAUDE_CODE_USE_VERTEX"),
                                     (w.claude_uses_foundry, "CLAUDE_CODE_USE_FOUNDRY")])
def test_claude_uses_vertex_and_foundry(monkeypatch, fn, var):
    assert fn({}) is False
    assert fn({var: "0"}) is False
    assert fn({var: "1"}) is True
    assert fn() is False
    monkeypatch.setenv(var, "yes")
    assert fn() is True
