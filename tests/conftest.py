"""Shared fixtures: every test runs against an empty, throwaway home directory with no network."""

from __future__ import annotations

import subprocess
import sys
import types
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import usage_widget as w  # noqa: E402

# Environment variables the app reads; cleared so the developer's or runner's own logins never leak in.
APP_ENV = (
    "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_BASE_URL",
    "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A fake home with empty Claude, Codex and widget config dirs; network and Keychain blocked."""
    home = tmp_path / "home"
    home.mkdir()
    for var in APP_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    # boto3 must not find real AWS credentials or instance metadata.
    monkeypatch.setenv("AWS_CONFIG_FILE", str(home / ".aws" / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(home / ".aws" / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(w, "_SSL_CONTEXT", None)

    def no_network(*args, **kwargs):
        raise AssertionError("tests must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    # The macOS Keychain lookup: "not found" unless a test says otherwise.
    # A stand-in module so tests can swap w.subprocess.run without touching the real subprocess.
    fake_subprocess = types.SimpleNamespace(**{k: getattr(subprocess, k) for k in dir(subprocess)
                                               if not k.startswith("_")})
    fake_subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 44, stdout="", stderr="")
    monkeypatch.setattr(w, "subprocess", fake_subprocess)
    return home


@pytest.fixture
def home(isolated) -> Path:
    return isolated


@pytest.fixture
def fake_http(monkeypatch):
    """Replace http_json with canned responses keyed by URL; records each call."""

    class FakeHttp:
        def __init__(self):
            self.responses: dict[str, object] = {}
            self.calls: list[tuple[str, dict]] = []

        def __call__(self, url, headers, timeout=20):
            self.calls.append((url, headers))
            response = self.responses[url]
            if isinstance(response, BaseException):
                raise response
            return response

    fake = FakeHttp()
    monkeypatch.setattr(w, "http_json", fake)
    return fake


@pytest.fixture
def frozen_now(monkeypatch):
    """Pin datetime.now() inside usage_widget; call with a UTC datetime."""

    def freeze(when: datetime):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return when.astimezone(tz) if tz else when.astimezone().replace(tzinfo=None)

        monkeypatch.setattr(w, "datetime", Frozen)
        return when

    return freeze

