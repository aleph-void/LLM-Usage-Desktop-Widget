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


def pytest_report_header(config):
    """Which Tcl/Tk the Tk tests run on; its Aqua build is the usual suspect when they hang on macOS."""
    try:
        import tkinter

        tcl = tkinter.Tcl()  # a bare Tcl interpreter: no window, so nothing to hang or crash on
        return f"tcl/tk: {tcl.eval('info patchlevel')} from {tcl.eval('info library')}"
    except Exception as e:  # no tkinter at all, or a broken install
        return f"tcl/tk: unavailable ({e})"

# Environment variables the app reads; cleared so the developer's or runner's own logins never leak in.
APP_ENV = (
    "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_BASE_URL",
    "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_VERTEX_PROJECT_ID", "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG", "AZURE_SUBSCRIPTION_ID",
    "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE",
    "OneDrive", "OneDriveConsumer", "OneDriveCommercial",  # synced settings must not reach the real OneDrive
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
    monkeypatch.setattr(w, "_TOKENS", {})
    monkeypatch.setattr(w, "_update_failed_at", 0.0)
    # No real gcloud / az, and no google-auth or azure-identity picking up real credentials.
    monkeypatch.setattr(w, "run_cli", lambda args, timeout=30: None)
    for mod in ("google.auth", "google.auth.transport.requests", "azure.identity"):
        monkeypatch.setitem(sys.modules, mod, None)

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
    """Replace http_json with canned responses; records each call.

    A response is looked up by exact URL, else by the longest matching URL prefix. It can be a value,
    an exception to raise, a callable(url, kwargs) or a list consumed one call at a time.
    """

    class FakeHttp:
        def __init__(self):
            self.responses: dict[str, object] = {}
            self.calls: list[tuple[str, dict]] = []
            self.kwargs: list[dict] = []

        def __call__(self, url, headers, timeout=20, **kwargs):
            self.calls.append((url, headers))
            self.kwargs.append(kwargs)
            key = url if url in self.responses else max(
                (k for k in self.responses if url.startswith(k)), key=len, default=None)
            if key is None:
                raise AssertionError(f"unexpected request: {url}")
            response = self.responses[key]
            if isinstance(response, list):
                response = response.pop(0)
            if callable(response) and not isinstance(response, BaseException):
                response = response(url, kwargs)
            if isinstance(response, BaseException):
                raise response
            return response

        def urls(self, prefix=""):
            return [url for url, _ in self.calls if url.startswith(prefix)]

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


@pytest.fixture
def newer_release(fake_http):
    """GitHub's latest release is v99.0.0, newer than this copy. Request it before a UI fixture:
    the UIs check for an update in their first refresh, while they are being built."""
    url = f"{w.REPO_URL}/releases/tag/v99.0.0"
    fake_http.responses[w.RELEASES_API] = {"tag_name": "v99.0.0", "html_url": url, "draft": False,
                                           "prerelease": False}
    return {"version": "99.0.0", "url": url}

