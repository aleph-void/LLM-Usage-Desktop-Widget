#!/usr/bin/env python3
"""Desktop widget showing Claude and Codex (ChatGPT) plan usage limits.

Reads the OAuth logins that Claude Code and the Codex CLI already store on
this machine, so there is nothing to configure: if `claude` and `codex` are
logged in, the widget works. Credentials are only ever read, never written,
and are sent only to the provider that issued them.

When Claude Code or Codex run on Amazon Bedrock (including the bedrock-mantle
endpoint), Google Vertex AI or Azure OpenAI / Microsoft Foundry there are no
plan limits; instead it shows that cloud's spend and token counts using your
AWS (needs boto3), Google Cloud or Azure credentials.

Appears where the system status icons live: the Linux top bar (AppIndicator,
via the system's PyGObject), the macOS menu bar and the Windows notification
area (pystray + Pillow). `--widget` shows a floating window that needs only
the standard library.

    python3 usage_widget.py            # tray / top bar / menu bar
    python3 usage_widget.py --widget   # floating window instead
    python3 usage_widget.py --once     # print usage to the terminal and exit
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "1.0.3"

APP_NAME = "llm-usage-widget"
USER_AGENT = f"llm-usage-widget/{__version__}"
REPO_URL = "https://github.com/aleph-void/LLM-Usage-Desktop-Widget"
LLC_URL = "https://alephvoid.com"
DEFAULT_INTERVAL = 180  # seconds between polls; the endpoints are not meant to be hammered
MIN_INTERVAL = 60


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Limit:
    label: str
    percent: float | None  # None: an informational row (spend, token counts) with no bar
    resets_at: datetime | None = None
    severity: str = ""  # "normal" | "warning" | "critical"; derived from percent if empty

    def __post_init__(self):
        if self.percent is not None and not math.isfinite(self.percent):
            raise ValueError("usage percentage must be finite")

    def level(self) -> str:
        if self.severity in ("warning", "critical"):
            return self.severity
        if self.percent is None:
            return "normal"
        if self.percent >= 90:
            return "critical"
        if self.percent >= 70:
            return "warning"
        return "normal"


@dataclass
class Credits:
    """Usage credits a provider reports, beyond its plan windows: pay-as-you-go spend (Claude's extra
    usage) or a balance that is drawn down (Codex). A credit cutoff stops the CLI at an amount of these."""
    used: float | None = None  # spent so far, when the provider counts upward
    left: float | None = None  # balance left, when the provider only reports what remains
    cap: float | None = None  # spending cap, if the provider has one
    unit: str = ""  # "$", or "" for plain credits
    what: str = "credits"  # how the menu names them: "extra usage", "credits left"

    def __post_init__(self):
        for value in (self.used, self.left, self.cap):
            if value is not None and not math.isfinite(value):
                raise ValueError("credit amounts must be finite")


@dataclass
class ProviderUsage:
    name: str
    plan: str = ""
    limits: list[Limit] = field(default_factory=list)
    error: str = ""
    fetched_at: float = 0.0
    summary: str = ""  # top-bar text when no limit has a percentage, e.g. "$12.34"
    account: str = ""  # whose numbers these are, so a synced cutoff only applies to that account
    credits: Credits | None = None  # usage credits, when the provider reports any


class AuthError(Exception):
    """Login missing or expired; the user has to act in the provider's CLI."""


class NotUsed(Exception):
    """The CLI is set up for another backend (e.g. Bedrock); leave this provider out."""


class HTTPStatusError(RuntimeError):
    def __init__(self, code: int, detail: str = ""):
        super().__init__(f"HTTP {code}" + (f": {detail}" if detail else ""))
        self.code = code


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    # Verify against the OS trust store (macOS Keychain, Windows cert store) when truststore is
    # installed. A frozen app's bundled OpenSSL otherwise looks for CA certs under the build
    # machine's Python prefix, which doesn't exist on the user's Mac: CERTIFICATE_VERIFY_FAILED.
    try:
        import truststore
    except ImportError:
        return ssl.create_default_context()
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


_SSL_CONTEXT: ssl.SSLContext | None = None
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PAGES = 100


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API URLs are fixed. Never forward bearer tokens or OAuth POST bodies elsewhere.
        return None


def _urlopen(req, timeout, context):
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))
    return opener.open(req, timeout=timeout)


def _https_origin(url: str) -> tuple[str, int]:
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or any(ord(c) <= 32 for c in url)):
        raise ValueError("API URL must use HTTPS without user information or whitespace")
    return parts.hostname, parts.port or 443


def _read_response(resp, limit=MAX_RESPONSE_BYTES):
    raw = resp.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("API response exceeds size limit")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("API response must be a JSON object")
    return data


def http_json(url: str, headers: dict[str, str], timeout: float = 20, body: dict | None = None,
              form: dict | None = None, auth_errors: bool = True) -> dict:
    """GET (or POST `body` as JSON, `form` urlencoded) and parse the JSON reply.

    With auth_errors, 401/403 raise AuthError; otherwise every error status raises
    HTTPStatusError carrying the API's own message, so callers can tell them apart.
    """
    global _SSL_CONTEXT
    _https_origin(url)
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = _ssl_context()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with _urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
            return _read_response(resp)
    except urllib.error.HTTPError as e:
        try:
            if auth_errors and e.code in (401, 403):
                raise AuthError(f"HTTP {e.code}: login rejected") from e
            if auth_errors:
                raise RuntimeError(f"HTTP {e.code}") from e
            raise HTTPStatusError(e.code, _error_detail(e)) from e
        finally:
            # Python 3.9 leaves the file wrapper uninitialized when there is no body.
            if e.fp is not None:
                e.close()
    except urllib.error.URLError as e:
        raise RuntimeError(f"network: {e.reason}") from e


def _error_detail(e: urllib.error.HTTPError) -> str:
    """The message in a Google/Azure-style error body ({"error": {"message": ...}}), shortened."""
    if e.fp is None:
        return ""
    try:
        err = _read_response(e, 64 * 1024).get("error")
    except (OSError, ValueError, AttributeError):
        return ""
    if isinstance(err, dict):
        msg = str(err.get("message") or err.get("code") or "")
    else:
        msg = str(err or "")
    msg = " ".join(msg.split())
    return msg if len(msg) <= 90 else msg[:89] + "…"


def find_cli(name: str) -> str | None:
    """A command-line tool on PATH, or in the usual install folders a login-started app's PATH lacks."""
    found = shutil.which(name)
    if found:
        return found
    dirs = [Path.home() / "google-cloud-sdk" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
            Path("/snap/bin"), Path("/usr/lib/google-cloud-sdk/bin")]
    if sys.platform == "win32":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if os.environ.get(var):
                root = Path(os.environ[var])
                dirs += [root / "Microsoft SDKs" / "Azure" / "CLI2" / "wbin",
                         root / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin"]
    return shutil.which(name, path=os.pathsep.join(str(d) for d in dirs))


def run_cli(args: list[str], timeout: float = 30) -> str | None:
    """stdout of a CLI command, or None if it is missing or fails. No console window on Windows."""
    exe = find_cli(args[0])
    if not exe:
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run([exe, *args[1:]], capture_output=True, text=True, timeout=timeout,
                             creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def parse_time(value) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def format_remaining(when: datetime | None) -> str:
    if when is None:
        return ""
    secs = int((when - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "resetting"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def window_label(seconds) -> str:
    if not seconds:
        return "Limit"
    seconds = int(seconds)
    if seconds == 604800:
        return "Weekly"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


# On macOS and Windows the settings sync between computers through iCloud Drive and
# OneDrive, every one of them that is set up: config.json in each cloud folder holds
# everything but these keys, which describe this computer, plus "saved_at", so the
# newest copy wins and is copied to the others. A computer with both iCloud and
# OneDrive thereby links a Mac on iCloud only with a PC on OneDrive only. The local
# config.json keeps a full copy, used whenever no cloud folder is available.
# "sync": false in the local file turns syncing off.
LOCAL_ONLY_KEYS = ("x", "y", "taskbar_text", "collapsed", "sync", "cutoff_handled", "credit_cutoff_handled")


def cloud_config_dirs() -> list[Path]:
    """The iCloud Drive and OneDrive folders for synced settings, as many as are set up."""
    home = Path.home()
    roots = []
    if sys.platform == "darwin":
        roots.append(home / "Library" / "Mobile Documents" / "com~apple~CloudDocs")
        try:  # OneDrive on macOS 12+, one folder per account: OneDrive-Personal, OneDrive-<Company>
            roots += sorted(p for p in (home / "Library" / "CloudStorage").iterdir() if p.name.startswith("OneDrive"))
        except OSError:
            pass
        roots.append(home / "OneDrive")  # older OneDrive versions
    elif sys.platform == "win32":
        roots += [Path(os.environ[v]) for v in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
                  if os.environ.get(v)]
        roots.append(home / "iCloudDrive")  # iCloud for Windows
    found, seen = [], set()
    for root in roots:
        try:
            if not root.is_dir() or root.resolve() in seen:  # the OneDrive variables often name one folder
                continue
            seen.add(root.resolve())
        except OSError:
            continue
        found.append(root / APP_NAME)
    return found


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    # Replace in one step so a sync client never uploads a half-written file.
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids symlink clobbering and competing writers sharing a temp file.
    # NamedTemporaryFile also gives local settings and billing caches owner-only permissions.
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            tmp = Path(handle.name)
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _synced_paths(local: dict) -> list[Path]:
    if local.get("sync", True) is False:
        return []
    # Skip a folder where iCloud hasn't downloaded the file yet, rather than overwrite it.
    return [d / "config.json" for d in cloud_config_dirs() if not (d / ".config.json.icloud").exists()]


def _shared(cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if k not in LOCAL_ONLY_KEYS and k != "saved_at"}


def _write_copies(paths: list[Path], content: dict) -> None:
    """Write content to every cloud copy that differs, except ones that exist but can't be read (mid-sync)."""
    for path in paths:
        current = _read_json(path)
        if current == content or (current is None and path.exists()):
            continue
        try:
            _write_json(path, content)
        except OSError:
            pass


def _newest_copy(paths: list[Path]) -> dict | None:
    def age(item):
        path, copy = item
        saved = copy.get("saved_at")
        try:
            mtime = path.stat().st_mtime  # breaks ties, e.g. a copy edited by hand
        except OSError:
            mtime = 0.0
        return (saved if isinstance(saved, (int, float)) else 0.0, mtime)

    copies = [(p, c) for p, c in ((p, _read_json(p)) for p in paths) if c is not None]
    return max(copies, key=age)[1] if copies else None


def load_config() -> dict:
    local = _read_json(config_dir() / "config.json") or {}
    paths = _synced_paths(local)
    if not paths:
        return local
    newest = _newest_copy(paths)
    if newest is None:
        if any(p.exists() for p in paths) or not _shared(local):
            return local  # unreadable (mid-sync), or nothing to share yet: save_config creates the files
        newest = {**_shared(local), "saved_at": time.time()}  # first run: start from this computer's settings
    _write_copies(paths, newest)  # bring a cloud that missed a change up to date
    return {**_shared(newest), **{k: local[k] for k in LOCAL_ONLY_KEYS if k in local}}


def save_config(cfg: dict) -> None:
    try:
        _write_json(config_dir() / "config.json", cfg)
    except OSError:
        pass
    paths = _synced_paths(cfg)
    shared = _shared(cfg)
    copies = [_read_json(p) for p in paths]
    # Don't touch the clouds for a window move, or create the folders with nothing to sync.
    if all(c is not None and _shared(c) == shared for c in copies) or not (shared or any(p.exists() for p in paths)):
        return
    _write_copies(paths, {**shared, "saved_at": time.time()})


def update_config(**changes) -> dict:
    """Save these settings over a fresh read, so changes made elsewhere are kept; returns the config."""
    cfg = load_config()
    cfg.update(changes)
    save_config(cfg)
    return cfg


def _truthy(value) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


# --------------------------------------------------------------------------
# Claude (claude.ai subscription via Claude Code's OAuth login)
# --------------------------------------------------------------------------

def _claude_credentials() -> dict:
    sources = []
    if sys.platform == "darwin":
        # Claude Code keeps its login in the macOS Keychain.
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                sources.append(json.loads(out.stdout))
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    path = cfg / ".credentials.json"
    if path.exists():
        try:
            sources.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    for data in sources:
        if isinstance(data, dict) and data.get("claudeAiOauth", {}).get("accessToken"):
            return data["claudeAiOauth"]
    env = claude_code_env()
    if claude_uses_bedrock(env) or claude_uses_vertex(env) or claude_uses_foundry(env):
        raise NotUsed
    raise AuthError("not logged in — run `claude` and /login")


def claude_account() -> str:
    """Claude Code's signed-in account and organization, from its ~/.claude.json; "" if unknown."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(cfg) / ".claude.json" if cfg else Path.home() / ".claude.json"
    data = _read_json(path) or {}
    acct = data.get("oauthAccount") if isinstance(data.get("oauthAccount"), dict) else {}
    return "/".join(str(acct[k]) for k in ("accountUuid", "organizationUuid") if acct.get(k))


def claude_code_env() -> dict[str, str]:
    """This process's environment overlaid with the `env` block of Claude Code's settings."""
    env = dict(os.environ)
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    try:
        block = json.loads((cfg / "settings.json").read_text(encoding="utf-8")).get("env")
        if isinstance(block, dict):
            env.update({str(k): str(v) for k, v in block.items()})
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return env


def claude_uses_bedrock(env: dict[str, str] | None = None) -> bool:
    env = claude_code_env() if env is None else env
    return _truthy(env.get("CLAUDE_CODE_USE_BEDROCK")) or "bedrock" in env.get("ANTHROPIC_BASE_URL", "")


def claude_uses_vertex(env: dict[str, str] | None = None) -> bool:
    env = claude_code_env() if env is None else env
    return _truthy(env.get("CLAUDE_CODE_USE_VERTEX"))


def claude_uses_foundry(env: dict[str, str] | None = None) -> bool:
    """Claude Code on Microsoft Foundry (Azure)."""
    env = claude_code_env() if env is None else env
    return _truthy(env.get("CLAUDE_CODE_USE_FOUNDRY"))


def fetch_claude() -> ProviderUsage:
    creds = _claude_credentials()
    expires = creds.get("expiresAt")
    if expires and expires / 1000 < time.time():
        raise AuthError("login expired — open Claude Code to refresh it")
    data = http_json(
        "https://api.anthropic.com/api/oauth/usage",
        {"Authorization": f"Bearer {creds['accessToken']}", "anthropic-beta": "oauth-2025-04-20"},
    )
    usage = ProviderUsage("Claude", plan=str(creds.get("subscriptionType") or ""), account=claude_account())

    if isinstance(data.get("limits"), list) and data["limits"]:
        names = {"session": "Session (5h)", "weekly_all": "Weekly"}
        for item in data["limits"]:
            kind = item.get("kind", "")
            label = names.get(kind) or kind.replace("_", " ").capitalize()
            scope = item.get("scope") or {}
            model = (scope.get("model") or {}).get("display_name")
            surface = scope.get("surface")
            if kind == "weekly_scoped" or model or surface:
                label = "Weekly · " + str(model or surface or "scoped")
            usage.limits.append(Limit(label, float(item.get("percent") or 0),
                                      parse_time(item.get("resets_at")), item.get("severity") or ""))
    else:
        # Older response shape.
        for key, label in (("five_hour", "Session (5h)"), ("seven_day", "Weekly"),
                           ("seven_day_opus", "Weekly · Opus"), ("seven_day_sonnet", "Weekly · Sonnet")):
            item = data.get(key)
            if isinstance(item, dict) and item.get("utilization") is not None:
                usage.limits.append(Limit(label, float(item["utilization"]), parse_time(item.get("resets_at"))))

    extra = data.get("extra_usage") or {}
    if isinstance(extra, dict) and extra.get("is_enabled"):
        cap = _amount(extra.get("monthly_limit")) or None
        used = _amount(extra.get("used_credits"))
        if used is not None:  # extra usage is on, so a credit cutoff can be set, even before any is spent
            usage.credits = Credits(used=used, cap=cap, unit="$", what="extra usage")
        if used:
            pct = float(extra.get("utilization") or 0)
            label = f"Extra usage ${used:.2f}" + (f" / ${cap:.0f}" if cap else "")
            usage.limits.append(Limit(label, pct))
    return usage


def _amount(value) -> float | None:
    """A finite, non-negative number from a JSON number or numeric string (0 when missing), else None."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


# --------------------------------------------------------------------------
# Codex (ChatGPT subscription via the Codex CLI's login)
# --------------------------------------------------------------------------

def _codex_tokens() -> dict:
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if codex_cloud_urls():
            raise NotUsed from None
        raise AuthError("not logged in — run `codex login`") from None
    except (OSError, json.JSONDecodeError) as e:
        raise AuthError(f"cannot read {path}: {e}") from None
    tokens = data.get("tokens") or {}
    if not tokens.get("access_token"):
        if codex_cloud_urls():
            raise NotUsed
        if data.get("OPENAI_API_KEY"):
            raise AuthError("API-key login has no plan limits — use `codex login` with ChatGPT")
        raise AuthError("not logged in — run `codex login`")
    return tokens


def codex_base_urls() -> list[str]:
    """Every base_url in Codex's config.toml (model providers other than ChatGPT)."""
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return re.findall(r"""^\s*base_url\s*=\s*["']([^"']*)["']""", text, re.MULTILINE)


def codex_bedrock_urls() -> list[str]:
    """base_url values in Codex's config.toml that point at Bedrock (runtime or bedrock-mantle)."""
    return [u for u in codex_base_urls() if "bedrock" in u]


def codex_vertex_urls() -> list[str]:
    """base_url values that point at Vertex AI, e.g. https://us-central1-aiplatform.googleapis.com/..."""
    return [u for u in codex_base_urls() if "aiplatform.googleapis.com" in u]


AZURE_HOST_RE = re.compile(r"^https?://([a-z0-9][a-z0-9-]*)\.(?:openai\.azure\.com|cognitiveservices\.azure\.com"
                           r"|services\.ai\.azure\.com)(?=[:/?#]|$)", re.IGNORECASE)


def codex_azure_urls() -> list[str]:
    """base_url values that point at an Azure OpenAI / Foundry resource."""
    return [u for u in codex_base_urls() if AZURE_HOST_RE.match(u)]


def codex_cloud_urls() -> list[str]:
    return codex_bedrock_urls() + codex_vertex_urls() + codex_azure_urls()


def _codex_windows(prefix: str, rl: dict | None, out: list[Limit]) -> None:
    if not isinstance(rl, dict):
        return
    reached = rl.get("limit_reached")
    for key in ("primary_window", "secondary_window"):
        win = rl.get(key)
        if not isinstance(win, dict):
            continue
        label = window_label(win.get("limit_window_seconds"))
        if prefix:
            label = f"{prefix} · {label}"
        pct = float(win.get("used_percent") or 0)
        out.append(Limit(label, pct, parse_time(win.get("reset_at")),
                         "critical" if reached and pct >= 100 else ""))


def fetch_codex() -> ProviderUsage:
    tokens = _codex_tokens()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]
    data = http_json("https://chatgpt.com/backend-api/wham/usage", headers)
    usage = ProviderUsage("Codex", plan=str(data.get("plan_type") or ""), account=str(tokens.get("account_id") or ""))
    _codex_windows("", data.get("rate_limit"), usage.limits)
    _codex_windows("Code review", data.get("code_review_rate_limit"), usage.limits)
    extra = data.get("additional_rate_limits")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, dict):
                name = item.get("limit_name") or item.get("metered_feature") or item.get("name") or "Other"
                _codex_windows(str(name), item.get("rate_limit", item), usage.limits)
    credits = data.get("credits") or {}
    if isinstance(credits, dict) and credits.get("has_credits") and not credits.get("unlimited"):
        usage.limits.append(Limit(f"Credits: {credits.get('balance', '0')}", 0))
        balance = _amount(credits.get("balance"))
        if balance is not None:
            usage.credits = Credits(left=balance, what="credits left")
    return usage


# --------------------------------------------------------------------------
# Amazon Bedrock (pay-per-token via AWS credentials; also the bedrock-mantle endpoint)
# --------------------------------------------------------------------------
#
# Bedrock has no plan windows, so this shows what there is instead: spend against
# an AWS Budget (a real percentage, if one is configured), month-to-date Bedrock
# spend from Cost Explorer, and today's token counts from CloudWatch. It turns on
# by itself when Claude Code or Codex is set up for Bedrock; config.json can
# override or disable it:
#
#     "bedrock": {"profile": "work", "regions": ["us-east-1"], "budget": "bedrock-monthly",
#                 "cost_explorer": true, "cost_interval": 21600, "metrics_interval": 900}
#
# Cost Explorer charges $0.01 per request and billing data only updates a few
# times a day, so spend is cached for `cost_interval` seconds (default 6 h) and
# token counts for `metrics_interval` (default 15 min), across restarts too.

COST_INTERVAL = 6 * 3600
METRICS_INTERVAL = 15 * 60
AWS_REGION_RE = re.compile(r"\b[a-z]{2}(?:-gov)?-[a-z]+-\d\b")
AWS_AUTH_CODES = {"ExpiredToken", "ExpiredTokenException", "UnrecognizedClientException",
                  "InvalidClientTokenId", "InvalidSignatureException"}


def bedrock_settings() -> dict | None:
    """What to query, or None when nothing on this machine uses Bedrock."""
    cfg = load_config().get("bedrock")
    if cfg is False:
        return None
    env = claude_code_env()
    urls = codex_bedrock_urls()
    if not isinstance(cfg, dict):
        if not (cfg is True or claude_uses_bedrock(env) or urls):
            return None
        cfg = {}
    regions = list(cfg.get("regions") or [])
    if not regions:
        if claude_uses_bedrock(env):
            regions.append(env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "")
        for url in urls:
            m = AWS_REGION_RE.search(url)
            regions.append(m.group(0) if m else "")
    return {
        "profile": cfg.get("profile") or env.get("AWS_PROFILE") or None,
        "regions": list(dict.fromkeys(r for r in regions if r)),
        "budget": cfg.get("budget") or "",
        "cost_explorer": cfg.get("cost_explorer", True),
        "cost_interval": max(MIN_INTERVAL, int(cfg.get("cost_interval", COST_INTERVAL))),
        "metrics_interval": max(MIN_INTERVAL, int(cfg.get("metrics_interval", METRICS_INTERVAL))),
    }


_CACHE_LOCK = threading.RLock()


def _cache_entries(path: Path, now: float) -> dict:
    cache = _read_json(path) or {}
    return {k: v for k, v in cache.items() if isinstance(v, dict) and "value" in v
            and isinstance(v.get("at"), (int, float)) and math.isfinite(v["at"])
            and 0 <= now - v["at"] < 7 * 86400}


def _cached(key: str, max_age: float, compute):
    """compute() at most once per max_age seconds, remembered in a file across restarts."""
    path = config_dir() / "cloud-cache.json"
    now = time.time()
    with _CACHE_LOCK:
        entry = _cache_entries(path, now).get(key)
        if entry is not None and now - entry["at"] < max_age:
            return entry["value"]
    value = compute()
    with _CACHE_LOCK:
        # Re-read after the query: another provider may have populated its cache meanwhile.
        cache = _cache_entries(path, time.time())
        cache[key] = {"at": time.time(), "value": value}
        try:
            _write_json(path, cache)
        except OSError:
            pass
    return value


def _money(amount: float, unit: str) -> str:
    return f"${amount:,.2f}" if unit in ("", "USD") else f"{amount:,.2f} {unit}"


def _human(n: float) -> str:
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= size:
            return f"{n / size:.1f}{suffix}"
    return f"{n:.0f}"


def _next_period(unit: str | None) -> datetime | None:
    """When an AWS Budget of this TimeUnit starts its next period (budgets run on UTC)."""
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "DAILY":
        return day + timedelta(days=1)
    if unit == "MONTHLY":
        return day.replace(year=now.year + now.month // 12, month=now.month % 12 + 1, day=1)
    if unit == "QUARTERLY":
        month = (now.month - 1) // 3 * 3 + 4
        return day.replace(year=now.year + (month > 12), month=(month - 1) % 12 + 1, day=1)
    if unit == "ANNUALLY":
        return day.replace(year=now.year + 1, month=1, day=1)
    return None


def _bedrock_budget(session, name: str) -> dict:
    account = session.client("sts").get_caller_identity()["Account"]
    budget = session.client("budgets", region_name="us-east-1").describe_budget(
        AccountId=account, BudgetName=name)["Budget"]
    spend = (budget.get("CalculatedSpend") or {}).get("ActualSpend") or {}
    return {"spent": float(spend.get("Amount") or 0), "limit": float(budget["BudgetLimit"]["Amount"]),
            "unit": budget["BudgetLimit"].get("Unit", "USD"), "time_unit": budget.get("TimeUnit")}


def _bedrock_month_cost(session) -> dict:
    """Month-to-date cost of every billed service with Bedrock in its name.

    Third-party models (Claude among them) bill as Marketplace services named
    like "Claude Sonnet 4.5 (Amazon Bedrock Edition)", not as "Amazon Bedrock".
    """
    ce = session.client("ce", region_name="us-east-1")
    today = datetime.now(timezone.utc).date()
    request = {
        "TimePeriod": {"Start": today.replace(day=1).isoformat(), "End": (today + timedelta(days=1)).isoformat()},
        "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    total, unit = 0.0, "USD"
    seen = set()
    while True:
        resp = ce.get_cost_and_usage(**request)
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                if "bedrock" in group["Keys"][0].lower():
                    cost = group["Metrics"]["UnblendedCost"]
                    total += float(cost["Amount"])
                    unit = cost.get("Unit") or unit
        if not resp.get("NextPageToken"):
            return {"amount": total, "unit": unit}
        token = resp["NextPageToken"]
        if token in seen or len(seen) >= MAX_PAGES - 1:
            raise RuntimeError("Cost Explorer pagination did not finish within the page limit")
        seen.add(token)
        request["NextPageToken"] = token


def _bedrock_tokens(session, region: str, since: datetime) -> dict:
    """Input/output tokens since `since`, bedrock-runtime and bedrock-mantle combined."""
    def search(metric):
        return f"SUM(SEARCH('{{AWS/Bedrock,ModelId}} MetricName=\"{metric}\"', 'Sum', 300))"

    def stat(metric):
        return {"Metric": {"Namespace": "AWS/BedrockMantle", "MetricName": metric}, "Period": 300, "Stat": "Sum"}

    queries = [
        {"Id": "runtime_in", "Expression": search("InputTokenCount")},
        {"Id": "runtime_out", "Expression": search("OutputTokenCount")},
        {"Id": "mantle_in", "MetricStat": stat("TotalInputTokens")},
        {"Id": "mantle_out", "MetricStat": stat("TotalOutputTokens")},
    ]
    totals = {"in": 0.0, "out": 0.0}
    pages = session.client("cloudwatch", region_name=region).get_paginator("get_metric_data").paginate(
        MetricDataQueries=queries, StartTime=since, EndTime=datetime.now(timezone.utc))
    for index, page in enumerate(pages):
        if index >= MAX_PAGES:
            raise RuntimeError("CloudWatch pagination did not finish within the page limit")
        for result in page.get("MetricDataResults", []):
            totals[result["Id"].rsplit("_", 1)[1]] += sum(result.get("Values", []))
    return totals


def fetch_bedrock() -> ProviderUsage:
    settings = bedrock_settings()
    if settings is None:
        raise NotUsed
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound
    except ImportError:
        raise RuntimeError("needs boto3 — pip install boto3 (Debian/Ubuntu: apt install python3-boto3)") from None

    profile = settings["profile"]
    login = "aws sso login" + (f" --profile {profile}" if profile else "")
    try:
        session = boto3.Session(profile_name=profile)
    except ProfileNotFound:
        raise AuthError(f"AWS profile {profile!r} not found") from None
    if session.get_credentials() is None:
        raise AuthError(f"no AWS credentials — run `{login}` or set a profile")
    regions = settings["regions"] or [session.region_name or "us-east-1"]
    usage = ProviderUsage("Bedrock")
    who = profile or "default"
    try:  # the AWS account, so a synced cutoff only applies to it
        usage.account = str(_cached(f"account:{who}", 86400,
                                    lambda: session.client("sts").get_caller_identity()["Account"]))
    except Exception:
        pass
    month = datetime.now(timezone.utc).strftime("%Y-%m")

    def attempt(what, fn):
        """Run one query; credential problems fail the provider, anything else becomes a note."""
        try:
            return fn()
        except NoCredentialsError:
            raise AuthError(f"no AWS credentials — run `{login}`") from None
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in AWS_AUTH_CODES:
                raise AuthError(f"AWS credentials expired — run `{login}`") from None
            usage.limits.append(Limit(f"⚠ {what}: {code or e}", None))
        except BotoCoreError as e:
            # Expired SSO sessions surface as TokenRetrievalError / SSOTokenLoadError.
            if "token" in type(e).__name__.lower():
                raise AuthError(f"AWS SSO session expired — run `{login}`") from None
            usage.limits.append(Limit(f"⚠ {what}: {e}", None))
        return None

    if settings["budget"]:
        name = settings["budget"]
        b = attempt("budget", lambda: _cached(f"budget:{who}:{name}", settings["cost_interval"],
                                              lambda: _bedrock_budget(session, name)))
        if b:
            pct = b["spent"] / b["limit"] * 100 if b["limit"] else 0.0
            usage.limits.append(Limit(f"Budget {_money(b['spent'], b['unit'])} / {_money(b['limit'], b['unit'])}",
                                      pct, _next_period(b["time_unit"])))

    if settings["cost_explorer"]:
        cost = attempt("Cost Explorer", lambda: _cached(f"cost:{who}:{month}", settings["cost_interval"],
                                                        lambda: _bedrock_month_cost(session)))
        if cost:
            usage.summary = _money(cost["amount"], cost["unit"])
            usage.limits.append(Limit(f"This month {usage.summary}", None, _next_period("MONTHLY")))

    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    tokens_in = tokens_out = 0.0
    got_tokens = False
    for region in regions:
        t = attempt(f"CloudWatch {region}", lambda r=region: _cached(
            f"tokens:{who}:{r}:{midnight.date()}", settings["metrics_interval"],
            lambda: _bedrock_tokens(session, r, midnight)))
        if t:
            got_tokens = True
            tokens_in += t["in"]
            tokens_out += t["out"]
    if got_tokens:
        usage.limits.append(Limit(f"Tokens today {_human(tokens_in)} in · {_human(tokens_out)} out", None))
        if not usage.summary:
            usage.summary = f"{_human(tokens_in + tokens_out)} tok"
    return usage


def _http_attempt(usage: ProviderUsage, expired: str):
    """attempt(what, fn) for the REST-based clouds, like Bedrock's: a rejected login
    fails the provider, anything else (a missing permission, say) becomes a note."""
    def attempt(what, fn):
        try:
            return fn()
        except HTTPStatusError as e:
            if e.code == 401:
                raise AuthError(expired) from None
            usage.limits.append(Limit(f"⚠ {what}: {e}", None))
        except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:
            usage.limits.append(Limit(f"⚠ {what}: {e or type(e).__name__}", None))
        return None
    return attempt


def _rfc3339(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Access tokens fetched this run, by cloud: (token, expires_at, extra).
_TOKENS: dict[str, tuple[str, float, str]] = {}


# --------------------------------------------------------------------------
# Google Vertex AI (pay-per-token via Google Cloud credentials)
# --------------------------------------------------------------------------
#
# Like Bedrock there are no plan windows. This shows today's token counts from
# Cloud Monitoring and, when a Cloud Billing export to BigQuery is set up,
# month-to-date Vertex AI spend, optionally against a monthly budget amount
# (Google's Budgets API doesn't report spend, so the amount is given here). It
# turns on by itself when Claude Code (CLAUDE_CODE_USE_VERTEX) or a Codex
# base_url uses Vertex AI; config.json can override or disable it:
#
#     "vertex": {"projects": ["my-project"], "billing_table": "my-project.billing.gcp_billing_export_v1_XXXX",
#                "budget": 500, "cost_interval": 21600, "metrics_interval": 900}
#
# Credentials are Application Default Credentials (`gcloud auth application-default
# login`), with google-auth or the gcloud CLI as fallbacks for other ADC types.

GCP_LOGIN = "gcloud auth application-default login"
GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
VERTEX_PROJECT_RE = re.compile(r"/projects/([^/]+)/")
VERTEX_TOKEN_METRIC = "aiplatform.googleapis.com/publisher/online_serving/token_count"
BQ_TABLE_RE = re.compile(r"^[\w.:-]+\.[\w-]+\.[\w-]+$")


def vertex_settings() -> dict | None:
    """What to query, or None when nothing on this machine uses Vertex AI."""
    cfg = load_config().get("vertex")
    if cfg is False:
        return None
    env = claude_code_env()
    urls = codex_vertex_urls()
    if not isinstance(cfg, dict):
        if not (cfg is True or claude_uses_vertex(env) or urls):
            return None
        cfg = {}
    projects = list(cfg.get("projects") or ([cfg["project"]] if cfg.get("project") else []))
    if not projects:
        if claude_uses_vertex(env):
            projects.append(env.get("ANTHROPIC_VERTEX_PROJECT_ID") or env.get("GOOGLE_CLOUD_PROJECT")
                            or env.get("GCLOUD_PROJECT") or "")
        for url in urls:
            m = VERTEX_PROJECT_RE.search(url)
            projects.append(m.group(1) if m else "")
    return {
        "projects": list(dict.fromkeys(str(p) for p in projects if p)),
        "billing_table": str(cfg.get("billing_table") or ""),
        "budget": float(cfg.get("budget") or 0),
        "cost_interval": max(MIN_INTERVAL, int(cfg.get("cost_interval", COST_INTERVAL))),
        "metrics_interval": max(MIN_INTERVAL, int(cfg.get("metrics_interval", METRICS_INTERVAL))),
    }


def _gcp_adc() -> dict:
    """The Application Default Credentials file that `gcloud auth application-default login` writes."""
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        if os.environ.get("CLOUDSDK_CONFIG"):
            base = Path(os.environ["CLOUDSDK_CONFIG"])
        elif sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "gcloud"
        else:
            base = Path.home() / ".config" / "gcloud"
        path = base / "application_default_credentials.json"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _gcp_token(adc: dict) -> str:
    cached = _TOKENS.get("gcp")
    if cached and cached[1] - 60 > time.time():
        return cached[0]
    token, ttl = "", 3000.0
    if adc.get("type") == "authorized_user" and adc.get("refresh_token"):
        # A user login: trade the refresh token for an access token at Google, which issued it.
        try:
            resp = http_json("https://oauth2.googleapis.com/token", {}, auth_errors=False, form={
                "grant_type": "refresh_token", "client_id": adc.get("client_id", ""),
                "client_secret": adc.get("client_secret", ""), "refresh_token": adc["refresh_token"]})
        except HTTPStatusError as e:
            if e.code in (400, 401):
                raise AuthError(f"Google login expired — run `{GCP_LOGIN}`") from None
            raise
        token, ttl = str(resp.get("access_token") or ""), float(resp.get("expires_in") or ttl)
    if not token:
        # Service accounts, workload identity and the like need google-auth to sign requests.
        try:
            import google.auth
            import google.auth.transport.requests
            creds, _ = google.auth.default(scopes=[GCP_SCOPE])
            creds.refresh(google.auth.transport.requests.Request())
            token = str(creds.token or "")
        except Exception:
            pass
    if not token:
        out = (run_cli(["gcloud", "auth", "application-default", "print-access-token"])
               or run_cli(["gcloud", "auth", "print-access-token"]))
        token = (out or "").strip()
    if not token:
        raise AuthError(f"no Google Cloud credentials — run `{GCP_LOGIN}`")
    _TOKENS["gcp"] = (token, time.time() + ttl, "")
    return token


def _vertex_tokens(headers: dict, project: str, since: datetime) -> dict:
    """Input/output tokens since `since` across every publisher model (Claude, Gemini, …) in the project."""
    params = {
        "filter": f'metric.type="{VERTEX_TOKEN_METRIC}"',
        "interval.startTime": _rfc3339(since),
        "interval.endTime": _rfc3339(datetime.now(timezone.utc)),
        "aggregation.alignmentPeriod": "3600s",
        "aggregation.perSeriesAligner": "ALIGN_SUM",
        "aggregation.crossSeriesReducer": "REDUCE_SUM",
        "aggregation.groupByFields": "metric.label.type",
    }
    url = f"https://monitoring.googleapis.com/v3/projects/{urllib.parse.quote(project)}/timeSeries?"
    totals = {"in": 0.0, "out": 0.0}
    seen = set()
    while True:
        data = http_json(url + urllib.parse.urlencode(params), headers, auth_errors=False)
        for series in data.get("timeSeries", []):
            key = {"input": "in", "output": "out"}.get(series.get("metric", {}).get("labels", {}).get("type"))
            if key:
                for point in series.get("points", []):
                    value = point.get("value", {})
                    totals[key] += float(value.get("int64Value") or value.get("doubleValue") or 0)
        if not data.get("nextPageToken"):
            return totals
        token = data["nextPageToken"]
        if token in seen or len(seen) >= MAX_PAGES - 1:
            raise RuntimeError("Monitoring pagination did not finish within the page limit")
        seen.add(token)
        params["pageToken"] = token


def _vertex_month_cost(headers: dict, table: str, projects: list[str]) -> dict:
    """Month-to-date Vertex AI cost (after credits) of these projects, from the BigQuery billing export.

    Claude and other partner models can bill under their own service names, so
    those count too, not only "Vertex AI".
    """
    if not BQ_TABLE_RE.match(table):
        raise ValueError("billing_table should look like project.dataset.table")
    today = datetime.now(timezone.utc).date()
    sql = (f"SELECT SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), "
           f"ANY_VALUE(currency) FROM `{table}` "
           "WHERE export_time >= TIMESTAMP(@start) AND invoice.month = @month AND project.id IN UNNEST(@projects) "
           "AND REGEXP_CONTAINS(LOWER(service.description), r'vertex|claude|anthropic|gemini')")

    def param(name, value):
        return {"name": name, "parameterType": {"type": "STRING"}, "parameterValue": {"value": value}}

    body = {
        "query": sql, "useLegacySql": False, "parameterMode": "NAMED", "timeoutMs": 30000,
        "queryParameters": [
            param("start", today.replace(day=1).isoformat()),
            param("month", today.strftime("%Y%m")),
            {"name": "projects", "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
             "parameterValue": {"arrayValues": [{"value": p} for p in projects]}},
        ],
    }
    url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{urllib.parse.quote(projects[0])}/queries"
    data = http_json(url, headers, timeout=40, body=body, auth_errors=False)
    if not data.get("jobComplete"):
        raise RuntimeError("billing query still running")
    cells = (data.get("rows") or [{"f": [{}, {}]}])[0]["f"]
    return {"amount": float(cells[0].get("v") or 0), "unit": cells[1].get("v") or "USD"}


def fetch_vertex() -> ProviderUsage:
    settings = vertex_settings()
    if settings is None:
        raise NotUsed
    adc = _gcp_adc()
    headers = {"Authorization": f"Bearer {_gcp_token(adc)}"}
    if adc.get("quota_project_id"):
        headers["x-goog-user-project"] = adc["quota_project_id"]
    projects = settings["projects"]
    if not projects:
        fallback = adc.get("quota_project_id") or (run_cli(["gcloud", "config", "get-value", "project"]) or "").strip()
        projects = [fallback] if fallback else []
    if not projects:
        raise RuntimeError('no Google Cloud project — set "projects" in the vertex section of config.json')
    usage = ProviderUsage("Vertex", account=",".join(sorted(projects)))
    attempt = _http_attempt(usage, f"Google login expired — run `{GCP_LOGIN}`")
    who = ",".join(projects)
    month = datetime.now(timezone.utc).strftime("%Y-%m")

    table = settings["billing_table"]
    if table:
        cost = attempt("billing export", lambda: _cached(f"vertex:cost:{who}:{table}:{month}", settings["cost_interval"],
                                                         lambda: _vertex_month_cost(headers, table, projects)))
        if cost:
            usage.summary = _money(cost["amount"], cost["unit"])
            budget = settings["budget"]
            if budget:
                usage.limits.append(Limit(f"This month {usage.summary} / {_money(budget, cost['unit'])}",
                                          cost["amount"] / budget * 100, _next_period("MONTHLY")))
            else:
                usage.limits.append(Limit(f"This month {usage.summary}", None, _next_period("MONTHLY")))

    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    tokens_in = tokens_out = 0.0
    got_tokens = False
    for project in projects:
        t = attempt(f"Monitoring {project}", lambda p=project: _cached(
            f"vertex:tokens:{p}:{midnight.date()}", settings["metrics_interval"],
            lambda: _vertex_tokens(headers, p, midnight)))
        if t:
            got_tokens = True
            tokens_in += t["in"]
            tokens_out += t["out"]
    if got_tokens:
        usage.limits.append(Limit(f"Tokens today {_human(tokens_in)} in · {_human(tokens_out)} out", None))
        if not usage.summary:
            usage.summary = f"{_human(tokens_in + tokens_out)} tok"
    return usage


# --------------------------------------------------------------------------
# Azure OpenAI / Microsoft Foundry (pay-per-token via Azure credentials)
# --------------------------------------------------------------------------
#
# Spend against an Azure budget (a real percentage: the Consumption API reports
# current spend), month-to-date cost of the Azure OpenAI / Foundry resources
# from Cost Management, and today's token counts from Azure Monitor. It turns on
# by itself when a Codex base_url points at an Azure resource (*.openai.azure.com,
# *.cognitiveservices.azure.com, *.services.ai.azure.com) or Claude Code uses
# Foundry (CLAUDE_CODE_USE_FOUNDRY); config.json can override or disable it:
#
#     "azure": {"subscription": "<id>", "resources": ["my-openai"], "budget": "ai-monthly",
#               "cost_management": true, "cost_interval": 21600, "metrics_interval": 900}
#
# Credentials come from the Azure CLI (`az login`), or azure-identity if installed.

ARM = "https://management.azure.com"
AZURE_LOGIN = "az login"
AZURE_KINDS = {"openai", "aiservices"}
# Azure OpenAI's token metrics, then the newer Foundry "model usage" ones for resources without them.
AZURE_TOKEN_METRICS = (("ProcessedPromptTokens", "GeneratedTokens"), ("InputTokens", "OutputTokens"))
AZURE_TIME_GRAINS = {"Monthly": "MONTHLY", "BillingMonth": "MONTHLY", "Quarterly": "QUARTERLY",
                     "BillingQuarter": "QUARTERLY", "Annually": "ANNUALLY", "BillingAnnual": "ANNUALLY"}


def azure_settings() -> dict | None:
    """What to query, or None when nothing on this machine uses Azure OpenAI or Foundry."""
    cfg = load_config().get("azure")
    if cfg is False:
        return None
    env = claude_code_env()
    urls = codex_azure_urls()
    foundry = claude_uses_foundry(env)
    if not isinstance(cfg, dict):
        if not (cfg is True or foundry or urls):
            return None
        cfg = {}
    resources = list(cfg.get("resources") or [])
    if not resources:
        if foundry:
            m = AZURE_HOST_RE.match(env.get("ANTHROPIC_FOUNDRY_BASE_URL", ""))
            resources.append(env.get("ANTHROPIC_FOUNDRY_RESOURCE") or (m.group(1) if m else ""))
        for url in urls:
            resources.append(AZURE_HOST_RE.match(url).group(1))
    return {
        "subscription": str(cfg.get("subscription") or env.get("AZURE_SUBSCRIPTION_ID") or ""),
        "resources": list(dict.fromkeys(str(r) for r in resources if r)),
        "budget": str(cfg.get("budget") or ""),
        "cost_management": cfg.get("cost_management", True),
        "cost_interval": max(MIN_INTERVAL, int(cfg.get("cost_interval", COST_INTERVAL))),
        "metrics_interval": max(MIN_INTERVAL, int(cfg.get("metrics_interval", METRICS_INTERVAL))),
    }


def _azure_token() -> tuple[str, str]:
    """(Azure Resource Manager access token, the Azure CLI's current subscription or "")."""
    cached = _TOKENS.get("azure")
    if cached and cached[1] - 60 > time.time():
        return cached[0], cached[2]
    token, expires, sub = "", time.time() + 3000, ""
    out = run_cli(["az", "account", "get-access-token", "--resource", ARM + "/", "--output", "json"])
    if out:
        try:
            data = json.loads(out)
            token, sub = str(data.get("accessToken") or ""), str(data.get("subscription") or "")
            expires = float(data.get("expires_on") or expires)
        except (ValueError, AttributeError):
            token = ""
    if not token:
        try:
            from azure.identity import DefaultAzureCredential
            access = DefaultAzureCredential().get_token(ARM + "/.default")
            token, expires = access.token, float(access.expires_on)
        except Exception:
            pass
    if not token:
        raise AuthError(f"no Azure credentials — run `{AZURE_LOGIN}`")
    _TOKENS["azure"] = (token, expires, sub)
    return token, sub


def _arm_list(headers: dict, path: str) -> list[dict]:
    url, items = ARM + path, []
    seen = set()
    while url:
        if _https_origin(url) != _https_origin(ARM):
            raise ValueError("Azure pagination URL is outside Azure Resource Manager")
        if url in seen or len(seen) >= MAX_PAGES:
            raise RuntimeError("Azure pagination did not finish within the page limit")
        seen.add(url)
        data = http_json(url, headers, auth_errors=False)
        items += data.get("value") or []
        url = data.get("nextLink")
    return items


def _azure_resource_ids(headers: dict, subscription: str, resources: list[str]) -> list[str]:
    """ARM IDs of the given accounts (names or IDs); every OpenAI / AI Services account if none are given."""
    ids = [r for r in resources if r.startswith("/")]
    names = {r.lower() for r in resources if not r.startswith("/")}
    if names or not resources:
        subs = [subscription] if subscription else [
            s["subscriptionId"] for s in _arm_list(headers, "/subscriptions?api-version=2022-12-01")]
        for sub in subs:
            for acct in _arm_list(headers, f"/subscriptions/{sub}/providers/Microsoft.CognitiveServices"
                                           "/accounts?api-version=2023-05-01"):
                subdomain = str((acct.get("properties") or {}).get("customSubDomainName") or "").lower()
                if (acct.get("name", "").lower() in names or subdomain in names
                        or (not resources and str(acct.get("kind", "")).lower() in AZURE_KINDS)):
                    ids.append(acct["id"])
    return list(dict.fromkeys(ids))


def _azure_tokens(headers: dict, resource_id: str, since: datetime) -> dict:
    """Input/output tokens since `since` for one Azure OpenAI / Foundry resource."""
    span = f"{_rfc3339(since)}/{_rfc3339(datetime.now(timezone.utc))}"
    error = None
    for names in AZURE_TOKEN_METRICS:
        query = urllib.parse.urlencode({"api-version": "2023-10-01", "metricnames": ",".join(names),
                                        "timespan": span, "interval": "PT1H", "aggregation": "Total"})
        try:
            data = http_json(f"{ARM}{resource_id}/providers/Microsoft.Insights/metrics?{query}",
                             headers, auth_errors=False)
        except HTTPStatusError as e:
            if e.code != 400:  # 400: this kind of resource doesn't have these metrics
                raise
            error = e
            continue
        totals = {"in": 0.0, "out": 0.0}
        for metric in data.get("value", []):
            key = "in" if (metric.get("name") or {}).get("value") == names[0] else "out"
            for series in metric.get("timeseries", []):
                totals[key] += sum(point.get("total") or 0 for point in series.get("data", []))
        return totals
    raise error


def _azure_month_cost(headers: dict, subscription: str, resource_ids: list[str]) -> dict:
    """Month-to-date actual cost of these resources, from Cost Management."""
    body = {
        "type": "ActualCost", "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            # Cost Management keeps resource IDs in lower case.
            "filter": {"dimensions": {"name": "ResourceId", "operator": "In",
                                      "values": [r.lower() for r in resource_ids]}},
        },
    }
    data = http_json(f"{ARM}/subscriptions/{subscription}/providers/Microsoft.CostManagement/query"
                     "?api-version=2023-03-01", headers, body=body, auth_errors=False)
    props = data.get("properties") or {}
    columns = props.get("columns") or []
    cost = next((i for i, c in enumerate(columns) if c.get("type") == "Number"), 0)
    currency = next((i for i, c in enumerate(columns) if c.get("name") == "Currency"), None)
    total, unit = 0.0, "USD"
    for row in props.get("rows") or []:
        total += float(row[cost] or 0)
        if currency is not None and row[currency]:
            unit = row[currency]
    return {"amount": total, "unit": unit}


def _azure_budget(headers: dict, subscription: str, name: str) -> dict:
    """A budget by name at subscription scope, or by its full ARM ID (e.g. on a resource group)."""
    path = name if name.startswith("/") else (
        f"/subscriptions/{subscription}/providers/Microsoft.Consumption/budgets/{urllib.parse.quote(name)}")
    props = http_json(f"{ARM}{path}?api-version=2023-05-01", headers, auth_errors=False)["properties"]
    spend = props.get("currentSpend") or {}
    return {"spent": float(spend.get("amount") or 0), "limit": float(props.get("amount") or 0),
            "unit": spend.get("unit") or "USD", "time_unit": AZURE_TIME_GRAINS.get(props.get("timeGrain"))}


def fetch_azure() -> ProviderUsage:
    settings = azure_settings()
    if settings is None:
        raise NotUsed
    token, cli_sub = _azure_token()
    headers = {"Authorization": f"Bearer {token}"}
    subscription = settings["subscription"] or cli_sub
    usage = ProviderUsage("Azure", account=":".join(filter(None, (subscription, ",".join(settings["resources"])))))
    attempt = _http_attempt(usage, f"Azure login expired — run `{AZURE_LOGIN}`")
    month = datetime.now(timezone.utc).strftime("%Y-%m")

    wanted = settings["resources"]
    ids = attempt("resources", lambda: _cached(
        f"azure:resources:{subscription}:{','.join(wanted)}", settings["cost_interval"],
        lambda: _azure_resource_ids(headers, subscription, wanted))) or []
    if not ids and not usage.limits:
        usage.limits.append(Limit("⚠ no Azure OpenAI resource found — set \"resources\" in config.json", None))
    by_sub: dict[str, list[str]] = {}
    for rid in ids:
        by_sub.setdefault(rid.split("/")[2], []).append(rid)

    budget_sub = subscription or next(iter(by_sub), "")
    if settings["budget"] and (budget_sub or settings["budget"].startswith("/")):
        name = settings["budget"]
        b = attempt("budget", lambda: _cached(f"azure:budget:{budget_sub}:{name}", settings["cost_interval"],
                                              lambda: _azure_budget(headers, budget_sub, name)))
        if b:
            pct = b["spent"] / b["limit"] * 100 if b["limit"] else 0.0
            usage.limits.append(Limit(f"Budget {_money(b['spent'], b['unit'])} / {_money(b['limit'], b['unit'])}",
                                      pct, _next_period(b["time_unit"])))

    if settings["cost_management"] and by_sub:
        total, unit, got_cost = 0.0, "USD", False
        for sub, rids in by_sub.items():
            key = f"azure:cost:{sub}:{','.join(sorted(r.lower() for r in rids))}:{month}"
            cost = attempt("Cost Management", lambda s=sub, r=rids, k=key: _cached(
                k, settings["cost_interval"], lambda: _azure_month_cost(headers, s, r)))
            if cost:
                got_cost = True
                total += cost["amount"]
                unit = cost["unit"]
        if got_cost:
            usage.summary = _money(total, unit)
            usage.limits.append(Limit(f"This month {usage.summary}", None, _next_period("MONTHLY")))

    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    tokens_in = tokens_out = 0.0
    got_tokens = False
    for rid in ids:
        t = attempt(f"Monitor {rid.rsplit('/', 1)[-1]}", lambda r=rid: _cached(
            f"azure:tokens:{r.lower()}:{midnight.date()}", settings["metrics_interval"],
            lambda: _azure_tokens(headers, r, midnight)))
        if t:
            got_tokens = True
            tokens_in += t["in"]
            tokens_out += t["out"]
    if got_tokens:
        usage.limits.append(Limit(f"Tokens today {_human(tokens_in)} in · {_human(tokens_out)} out", None))
        if not usage.summary:
            usage.summary = f"{_human(tokens_in + tokens_out)} tok"
    return usage


PROVIDERS = (("Claude", fetch_claude), ("Codex", fetch_codex), ("Bedrock", fetch_bedrock),
             ("Vertex", fetch_vertex), ("Azure", fetch_azure))


def fetch_all() -> list[ProviderUsage]:
    results = []
    for name, fn in PROVIDERS:
        try:
            u = fn()
        except NotUsed:
            continue
        except AuthError as e:
            u = ProviderUsage(name, error=str(e))
        except Exception as e:  # never let one provider break the widget
            u = ProviderUsage(name, error=str(e) or type(e).__name__)
        u.fetched_at = time.time()
        results.append(u)
    return results


# --------------------------------------------------------------------------
# Cutoff quotas: stop the CLIs before a limit runs out
# --------------------------------------------------------------------------
#
# Each provider can have its own cutoff, a percentage of its fullest limit. The first
# refresh that finds a provider at or over its cutoff stops the CLIs that draw on it
# (Claude Code for Claude, the Codex CLI for Codex, whichever of the two runs on
# the cloud for Bedrock, Vertex AI and Azure), then clears that cutoff so the CLIs can be started again.
# A provider that reports usage credits (Claude's extra usage, Codex's credit balance)
# can also have a credit cutoff: an amount of extra usage spent, or of credits left,
# that stops its CLI the same way. With synced settings every computer signed in to
# the same account stops its own CLIs; a cutoff set from the menu belongs to the account
# signed in there, so another computer's different account is left alone. In config.json
# a number applies to any account:
#
#     "cutoff": {"Claude": {"<account uuid>/<org uuid>": 95}, "Codex": 90, "Bedrock": 100}
#     "credit_cutoff": {"Claude": 20, "Codex": {"<account id>": 100}}

CUTOFF_STEP = 5  # the menu offers every multiple of this above the current usage, below 100
CREDIT_CUTOFF_KEY = "credit_cutoff"
CUTOFF_KEYS = ("cutoff", CREDIT_CUTOFF_KEY)
# Credit amounts the menu offers: 1, 1.5, 2, 3, 5, 7.5 in every power of ten up to 75 000.
CREDIT_CUTOFF_STEPS = tuple(float(m * 10 ** k) for k in range(5) for m in (1, 1.5, 2, 3, 5, 7.5))
CUTOFF_SYNC_WINDOW = 3600  # how long other computers act on a cutoff reached on one of them
# npm-installed CLIs run as `node <script>`; these mark the script's package.
CLI_PACKAGES = {"claude": "@anthropic-ai/claude-code/", "codex": "@openai/codex/"}
SCRIPT_RUNTIMES = {"node", "bun", "deno"}
# The Claude and Codex desktop apps have executables of the same names; leave them alone.
DESKTOP_APP_PATHS = (".app/contents/", "/anthropicclaude/", "/windowsapps/")


def cutoff_for(cfg: dict, name: str, account: str = "", key: str = "cutoff") -> float | None:
    """The cutoff set for this provider and account, or None if it has none.

    `key` is "cutoff" for the percentage of the fullest limit, CREDIT_CUTOFF_KEY for the credit amount.
    A number applies to any account; {account: number} only to those accounts.
    """
    cutoffs = cfg.get(key)
    value = cutoffs.get(name) if isinstance(cutoffs, dict) else None
    if isinstance(value, dict):
        value = value.get(account) if account else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if 0 < value <= (100 if key == "cutoff" else math.inf) else None


def _dict_setting(cfg: dict, key: str) -> dict:
    value = cfg.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _put_cutoff(cfg: dict, name: str, account: str, value: float | None, key: str = "cutoff") -> None:
    """Set (or with None, remove) a provider's cutoff for this account, or for any account without one."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # 20, not 20.0, in config.json
    cutoffs = _dict_setting(cfg, key)
    if account:
        per_account = _dict_setting(cutoffs, name)  # an any-account number gives way to this one
        per_account.pop(account, None)
        if value:
            per_account[account] = value
        cutoffs[name] = per_account
    elif value:
        cutoffs[name] = value
    else:
        cutoffs.pop(name, None)
    cutoffs = {k: v for k, v in cutoffs.items() if v != {}}
    if cutoffs:
        cfg[key] = cutoffs
    else:
        cfg.pop(key, None)


def set_cutoff(name: str, value: float | None, account: str = "", key: str = "cutoff") -> dict:
    """Save a provider's cutoff for an account (None turns it off); returns the updated config."""
    cfg = load_config()  # re-read so hand edits and other computers' changes are kept
    _put_cutoff(cfg, name, account, value, key)
    save_config(cfg)
    return cfg


def cutoff_choices(cfg: dict, u: ProviderUsage) -> list[int]:
    """The cutoffs the menu offers for a provider: off (0), then every CUTOFF_STEP above its usage, below 100.

    Anything at or below the fullest limit would be reached at once, so it isn't offered.
    A whole-number cutoff set by hand is listed too, so the menu can show it as chosen.
    """
    top = top_limit(u)
    used = top.percent if top is not None else 0
    choices = {v for v in range(CUTOFF_STEP, 100, CUTOFF_STEP) if v > used}
    current = cutoff_for(cfg, u.name, u.account)
    if current is not None and current > used and current.is_integer():
        choices.add(int(current))
    return [0, *sorted(choices)]


def cutoff_menu_providers(cfg: dict, data: list[ProviderUsage]) -> list[tuple[str, str, list[int]]]:
    """(provider, account, cutoff choices) of each provider shown, whose cutoff the menu offers."""
    return [(u.name, u.account, cutoff_choices(cfg, u)) for u in data]


def cutoff_menu_label(cfg: dict, name: str, account: str = "") -> str:
    cutoff = cutoff_for(cfg, name, account)
    return f"Stop {name} at: " + (f"{cutoff:.0f}%" if cutoff is not None else "off")


def credit_amount(value: float, unit: str = "") -> str:
    """An amount of credits for the menu: "$20", "$12.50", "100", "7.5"."""
    if float(value).is_integer():
        return f"{unit}{value:.0f}"
    return f"{unit}{value:.2f}" if unit == "$" else f"{unit}{value:g}"


def credits_reached(credits: Credits, cutoff: float) -> bool:
    """Whether the spend has reached this amount or, for a provider that only reports a balance, it has fallen to it."""
    if credits.used is not None:
        return credits.used >= cutoff
    return credits.left is not None and credits.left <= cutoff


def credit_cutoff_choices(cfg: dict, u: ProviderUsage) -> list[float]:
    """The credit cutoffs the menu offers for a provider with usage credits: off (0), then the amounts from
    CREDIT_CUTOFF_STEPS its credits have not reached.

    For spend that is above what is spent so far and below the cap, if there is one (without a cap: up to ten
    times the spend, or 100); for a balance, below what is left, down to a hundredth of it. Anything already
    reached would fire at once, so it isn't offered. A cutoff set by hand is listed too, so the menu can show it.
    """
    credits = u.credits
    if credits is None:
        return [0]
    if credits.used is not None:
        if credits.cap:
            choices = {v for v in CREDIT_CUTOFF_STEPS if credits.used < v < credits.cap}
        else:
            choices = {v for v in CREDIT_CUTOFF_STEPS if credits.used < v <= max(100.0, 10 * credits.used)}
    elif credits.left is not None:
        choices = {v for v in CREDIT_CUTOFF_STEPS if credits.left / 100 <= v < credits.left}
    else:
        choices = set()
    current = cutoff_for(cfg, u.name, u.account, CREDIT_CUTOFF_KEY)
    if current is not None and not credits_reached(credits, current):
        choices.add(current)
    return [0, *sorted(choices)]


def credit_cutoff_menu_label(cfg: dict, u: ProviderUsage) -> str:
    credits = u.credits or Credits()
    cutoff = cutoff_for(cfg, u.name, u.account, CREDIT_CUTOFF_KEY)
    return f"Stop {u.name} at {credits.what}: " + (credit_amount(cutoff, credits.unit) if cutoff is not None else "off")


def cutoff_menu_entries(cfg: dict, data: list[ProviderUsage]) -> list[tuple[str, str, str, str, list[tuple[float, str]]]]:
    """Every cutoff submenu to show: (config key, provider, account, menu label, [(value, item text), ...]).

    Each provider gets its percentage cutoff and, if it reports usage credits, its credit cutoff after it.
    """
    entries = []
    for u in data:
        entries.append(("cutoff", u.name, u.account, cutoff_menu_label(cfg, u.name, u.account),
                        [(v, f"{v}%" if v else "Off") for v in cutoff_choices(cfg, u)]))
        if u.credits is not None:
            entries.append((CREDIT_CUTOFF_KEY, u.name, u.account, credit_cutoff_menu_label(cfg, u),
                            [(v, credit_amount(v, u.credits.unit) if v else "Off")
                             for v in credit_cutoff_choices(cfg, u)]))
    return entries


def cutoff_reached(u: ProviderUsage, cutoff: float, key: str = "cutoff") -> bool:
    """Whether this provider's numbers are at or over a cutoff of the given kind."""
    if key == CREDIT_CUTOFF_KEY:
        return u.credits is not None and credits_reached(u.credits, cutoff)
    top = top_limit(u)
    return top is not None and top.percent >= cutoff


def cutoff_note(u: ProviderUsage, cutoff: float, key: str = "cutoff") -> str:
    """"Cutoff 95%" or "Credit cutoff $20", for the note left in the provider's limits."""
    if key == CREDIT_CUTOFF_KEY:
        return "Credit cutoff " + credit_amount(cutoff, u.credits.unit if u.credits else "")
    return f"Cutoff {cutoff:.0f}%"


def cutoff_clis(name: str) -> set[str]:
    """The CLIs that draw on this provider's quota."""
    if name == "Claude":
        return {"claude"}
    if name == "Codex":
        return {"codex"}
    claude, codex = {
        "Bedrock": (claude_uses_bedrock, codex_bedrock_urls),
        "Vertex": (claude_uses_vertex, codex_vertex_urls),
        "Azure": (claude_uses_foundry, codex_azure_urls),
    }.get(name, (None, None))
    if claude is None:
        return set()
    return ({"claude"} if claude() else set()) | ({"codex"} if codex() else set())


def cli_name(argv0: str, argv1: str = "") -> str | None:
    """"claude" or "codex" when a command line runs that CLI, else None."""
    path = argv0.replace("\\", "/")
    if any(p in path.lower() for p in DESKTOP_APP_PATHS):
        return None
    exe = path.rsplit("/", 1)[-1]
    if exe.lower().endswith(".exe"):
        exe = exe[:-4].lower()  # Windows names are case-insensitive; elsewhere "Claude" is the desktop app
    if exe in CLI_PACKAGES:
        return exe
    if exe.lower() in SCRIPT_RUNTIMES and argv1:
        script = argv1.replace("\\", "/").lower()
        base = script.rsplit("/", 1)[-1]
        for name, package in CLI_PACKAGES.items():
            if package in script or base in (name, name + ".js"):
                return name
    return None


def _first_args(cmdline: str) -> tuple[str, str]:
    """The first two arguments of a command line, honoring double quotes (Windows paths)."""
    args = [a or b for a, b in re.findall(r'"([^"]*)"|(\S+)', cmdline)[:2]]
    return (args + ["", ""])[0], (args + ["", ""])[1]


def _process_command_lines() -> list[tuple[int, str, str]]:
    """(pid, argv[0], argv[1]) of this user's processes."""
    procs = []
    if sys.platform == "win32":
        names = " OR ".join(f"Name='{n}.exe'" for n in (*CLI_PACKAGES, *SCRIPT_RUNTIMES))
        script = (f'Get-CimInstance Win32_Process -Filter "{names}" | '
                  'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }')
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in out.splitlines():
            pid, _, cmdline = line.partition("\t")
            if pid.strip().isdigit() and cmdline.strip():
                procs.append((int(pid), *_first_args(cmdline)))
    elif os.path.isdir("/proc/self"):
        uid = os.getuid()
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                argv = (Path(entry.path) / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue  # exited meanwhile
            argv = [a.decode(errors="replace") for a in argv] + ["", ""]
            if argv[0]:
                procs.append((int(entry.name), argv[0], argv[1]))
    else:
        out = subprocess.run(["ps", "-x", "-ww", "-o", "pid=,args="],
                             capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            pid, _, cmdline = line.strip().partition(" ")
            if pid.isdigit():
                procs.append((int(pid), *_first_args(cmdline)))
    return procs


def find_cli_processes() -> list[tuple[int, str]]:
    """(pid, "claude" | "codex") for each running Claude Code / Codex CLI process."""
    try:
        procs = _process_command_lines()
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for pid, argv0, argv1 in procs:
        name = cli_name(argv0, argv1)
        if name and pid != os.getpid():
            found.append((pid, name))
    return found


def stop_process(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError:
        return False
    return True


def enforce_cutoffs(results: list[ProviderUsage]) -> None:
    """Stop the CLIs of every provider at or over its cutoff (percentage or credits), then clear that cutoff.

    The cleared cutoff is recorded in "cutoff_reached" ("credit_cutoff_reached"), so other
    computers sharing the settings stop their CLIs too: once, if they see it within
    CUTOFF_SYNC_WINDOW. What happened is noted in the provider's limits.
    """
    cfg = load_config()
    now = time.time()
    todo = []  # (provider, kind of cutoff, cutoff, where it was reached)
    marks = {}  # kind: (reached, handled)
    for key in CUTOFF_KEYS:
        reached, handled = (_dict_setting(cfg, key + k) for k in ("_reached", "_handled"))
        for u in results:
            cutoff = cutoff_for(cfg, u.name, u.account, key)
            mark = reached.get(u.name)
            if cutoff is not None and cutoff_reached(u, cutoff, key):
                _put_cutoff(cfg, u.name, u.account, None, key)
                reached[u.name] = {"at": now, "cutoff": cutoff, "account": u.account}
                handled[u.name] = now
                todo.append((u, key, cutoff, ""))
            elif (isinstance(mark, dict) and all(isinstance(mark.get(k), (int, float)) for k in ("at", "cutoff"))
                  and mark.get("account", "") == u.account  # another computer's other account is none of ours
                  and abs(now - mark["at"]) < CUTOFF_SYNC_WINDOW and handled.get(u.name) != mark["at"]):
                handled[u.name] = mark["at"]
                todo.append((u, key, float(mark["cutoff"]), " on another computer"))
        marks[key] = (reached, handled)
    if not todo:
        return
    running = find_cli_processes()
    for u, key, cutoff, where in todo:
        clis = cutoff_clis(u.name)
        stopped = sum(stop_process(pid) for pid, name in running if name in clis)
        running = [(pid, name) for pid, name in running if name not in clis]  # signalled once, not again per cutoff
        what = " and ".join(sorted(clis)) or "CLI"
        if stopped:
            note = f"stopped {stopped} {what} process" + ("es" if stopped != 1 else "")
        else:
            note = f"no {what} running"
        u.limits.append(Limit(f"⛔ {cutoff_note(u, cutoff, key)} reached{where} — {note}; cutoff cleared", None))
    for key, (reached, handled) in marks.items():
        if reached or handled:
            cfg[key + "_reached"], cfg[key + "_handled"] = reached, handled
    save_config(cfg)


# --------------------------------------------------------------------------
# Update check
# --------------------------------------------------------------------------
#
# At most once a day the widget asks GitHub for the latest release and, if it is
# newer than this copy, offers it at the top of the menu. The answer is cached in
# cloud-cache.json, so restarts don't ask again. "update_check": false in
# config.json turns it off.

RELEASES_API = "https://api.github.com/repos/aleph-void/LLM-Usage-Desktop-Widget/releases/latest"
UPDATE_INTERVAL = 86400
UPDATE_RETRY = 3600  # after a failed check (offline, rate-limited), wait this long before trying again
VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)*)")
_update_failed_at = 0.0


def parse_version(text) -> tuple[int, ...] | None:
    """(1, 2, 3) for "v1.2.3" or "1.2.3"; None for anything else, including "1.2.3-rc1"."""
    m = VERSION_RE.fullmatch(str(text or "").strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def is_newer(version, than: str | None = None) -> bool:
    """Whether `version` is a later release than `than` (this copy by default), compared numerically."""
    new, old = parse_version(version), parse_version(__version__ if than is None else than)
    if new is None or old is None:
        return False
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def _latest_release() -> dict | None:
    """The newest published release as {"version", "url"}, or None if GitHub has none we can offer."""
    data = http_json(RELEASES_API, {"Accept": "application/vnd.github+json"}, timeout=10)
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "").strip()
    if parse_version(tag) is None:
        return None
    # Only ever open a page of this repo's releases, whatever the reply says.
    url = str(data.get("html_url") or "")
    if not url.startswith(REPO_URL + "/releases/"):
        url = f"{REPO_URL}/releases/tag/{urllib.parse.quote(tag)}"
    return {"version": tag.lstrip("v"), "url": url}


def check_for_update() -> dict | None:
    """The release to offer ({"version", "url"}) when one newer than this copy is out, else None.

    Never raises: a failed check must not break the widget. May block on the network, so
    call it from a worker thread.
    """
    global _update_failed_at
    if not _truthy(load_config().get("update_check", True)):
        return None
    if 0 <= time.time() - _update_failed_at < UPDATE_RETRY:
        return None
    try:
        latest = _cached("update:latest", UPDATE_INTERVAL, _latest_release)
    except Exception:  # offline, rate-limited, a reply that isn't JSON...
        _update_failed_at = time.time()
        return None
    if isinstance(latest, dict) and latest.get("url") and is_newer(latest.get("version")):
        # Disk caches are input too; construct the browser destination from a validated version.
        version = str(latest["version"]).strip().lstrip("v")
        canonical = f"{REPO_URL}/releases/tag/v{version}"
        url = latest["url"] if latest["url"] in (canonical, f"{REPO_URL}/releases/tag/{version}") else canonical
        return {"version": version, "url": url}
    return None


def update_label(update: dict) -> str:
    return f"Update available: v{update['version']}"


def notify_update(update: dict | None, notify=None) -> None:
    """Announce a newer release with a desktop notification, once per version.

    `notify(message, title)` is the tray icon's own notifier (pystray's balloon on Windows);
    otherwise macOS uses Notification Center and Linux notify-send. Call it off the UI thread.
    """
    if not update:
        return
    path = config_dir() / "update-notified"  # local, not config.json: no need to sync it
    try:
        if path.read_text(encoding="utf-8").strip() == update["version"]:
            return
    except OSError:
        pass
    title = "LLM Usage Widget"
    message = f"Version {update['version']} is available. Open the menu to get it."
    try:
        if notify:
            notify(message, title)
        elif sys.platform == "darwin":
            # Text passed as arguments, so nothing in it is parsed as AppleScript.
            subprocess.run(["osascript", "-e", "on run argv", "-e",
                            "display notification (item 1 of argv) with title (item 2 of argv)",
                            "-e", "end run", message, title], capture_output=True, timeout=10)
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            subprocess.run(["notify-send", "--app-name", title, title, message], capture_output=True, timeout=10)
        else:
            return  # nothing to show it with; the menu item and tooltip still say so
    except Exception:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(update["version"], encoding="utf-8")
    except OSError:
        pass

# --------------------------------------------------------------------------
# Terminal output
# --------------------------------------------------------------------------

def print_once() -> int:
    for u in fetch_all():
        print(f"{u.name}" + (f" ({u.plan})" if u.plan else ""))
        if u.error:
            print(f"  ! {u.error}")
        for lim in u.limits:
            reset = format_remaining(lim.resets_at)
            if lim.percent is None:
                print(f"  {lim.label}" + (f"  (resets {reset})" if reset else ""))
                continue
            bar = "#" * round(lim.percent / 5)
            print(f"  {lim.label:<24} {lim.percent:5.0f}%  [{bar:<20}]  {('resets ' + reset) if reset else ''}")
    return 0


# --------------------------------------------------------------------------
# Widget
# --------------------------------------------------------------------------

THEME = {
    "bg": "#1c1d21",
    "panel": "#26272d",
    "fg": "#e8e8ea",
    "muted": "#9a9ca5",
    "track": "#3a3c44",
    "normal": "#4caf50",
    "link": "#4fa3ff",
    "warning": "#f0b429",
    "critical": "#ef5350",
    "error": "#ff8a80",
    "claude": "#d97757",
    "codex": "#10a37f",
    "bedrock": "#ff9900",
    "vertex": "#4285f4",
    "azure": "#0078d4",
}
BAR_WIDTH = 210
BAR_HEIGHT = 6


class Widget:
    def __init__(self, interval: int, framed: bool):
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.interval = max(MIN_INTERVAL, interval)
        self.cfg = self._load_config()
        self.data: list[ProviderUsage] = []
        self.update: dict | None = None  # a newer release, from check_for_update()
        self.fetching = False
        self.last_fetch = 0.0

        root = self.root = tk.Tk()
        root.title("LLM Usage")
        root.configure(bg=THEME["bg"])
        root.resizable(False, False)
        if not framed:
            root.overrideredirect(True)
        root.attributes("-topmost", bool(self.cfg.get("topmost", True)))
        try:
            root.attributes("-alpha", 0.95)
        except tk.TclError:
            pass
        if "x" in self.cfg and "y" in self.cfg:
            root.geometry(f"+{int(self.cfg['x'])}+{int(self.cfg['y'])}")
        else:
            root.geometry(f"+{root.winfo_screenwidth() - 320}+60")

        base = tkfont.nametofont("TkDefaultFont").actual("family")
        self.f_title = tkfont.Font(family=base, size=10, weight="bold")
        self.f_label = tkfont.Font(family=base, size=9)
        self.f_small = tkfont.Font(family=base, size=8)

        self.header = tk.Frame(root, bg=THEME["bg"])
        self.header.pack(fill="x", padx=10, pady=(8, 2))
        self.status = tk.Label(self.header, text="Loading…", bg=THEME["bg"], fg=THEME["muted"], font=self.f_small)
        self.status.pack(side="left")
        for text, cmd in (("✕", self.quit), ("⟳", self.refresh)):
            b = tk.Label(self.header, text=text, bg=THEME["bg"], fg=THEME["muted"], font=self.f_label, cursor="hand2")
            b.pack(side="right", padx=(6, 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())
        # Shown next to the status while a newer release is out.
        self.update_link = tk.Label(self.header, text="", bg=THEME["bg"], fg=THEME["link"], font=self.f_small,
                                    cursor="hand2")
        self.update_link.bind("<Button-1>", lambda e: self._open_update())

        self.body = tk.Frame(root, bg=THEME["bg"])
        self.body.pack(fill="both", padx=10, pady=(0, 10))

        self.menu = tk.Menu(root, tearoff=0)
        self.topmost_var = tk.BooleanVar(value=bool(self.cfg.get("topmost", True)))
        self.cutoff_vars: list = []

        self._bind_drag(root)
        self.refresh()
        self._tick()

    # -- config --------------------------------------------------------------

    def _load_config(self) -> dict:
        return load_config()

    # -- window behaviour -----------------------------------------------------

    def _bind_drag(self, widget) -> None:
        widget.bind("<ButtonPress-1>", self._drag_start, add="+")
        widget.bind("<B1-Motion>", self._drag_move, add="+")
        widget.bind("<ButtonRelease-1>", self._drag_end, add="+")
        widget.bind("<Button-3>", self._popup, add="+")
        widget.bind("<Button-2>", self._popup, add="+")  # macOS secondary click
        for child in widget.winfo_children():
            if not isinstance(child, self.tk.Menu):
                self._bind_drag(child)

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if getattr(self, "_drag", None):
            dx, dy = self._drag
            self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _drag_end(self, _e):
        if getattr(self, "_drag", None):
            self._drag = None
            self.cfg = update_config(x=self.root.winfo_x(), y=self.root.winfo_y())

    def _build_menu(self):
        # Rebuilt on every popup so the cutoff entries follow the providers shown.
        tk, menu = self.tk, self.menu
        menu.delete(0, "end")
        for child in menu.winfo_children():
            child.destroy()
        self.cutoff_vars = []
        if self.update:
            menu.add_command(label=update_label(self.update), command=self._open_update)
            menu.add_separator()
        menu.add_command(label="Refresh now", command=self.refresh)
        menu.add_checkbutton(label="Always on top", variable=self.topmost_var, command=self._toggle_topmost)
        for key, name, account, label, choices in cutoff_menu_entries(self.cfg, self.data):
            var = tk.DoubleVar(value=cutoff_for(self.cfg, name, account, key) or 0.0)
            self.cutoff_vars.append(var)  # Tk forgets variables Python no longer references
            sub = tk.Menu(menu, tearoff=0)
            for value, text in choices:
                sub.add_radiobutton(label=text, variable=var, value=float(value),
                                    command=lambda k=key, n=name, a=account, v=value: self._set_cutoff(k, n, a, v))
            menu.add_cascade(label=label, menu=sub)
        about = tk.Menu(menu, tearoff=0)
        about.add_command(label=f"LLM Usage Widget — Version {__version__}", state="disabled")
        about.add_command(label=f"Source: {REPO_URL}", command=lambda: webbrowser.open(REPO_URL))
        about.add_command(label=f"Aleph Void LLC: {LLC_URL}", command=lambda: webbrowser.open(LLC_URL))
        menu.add_cascade(label="About", menu=about)
        menu.add_separator()
        menu.add_command(label="Quit", command=self.quit)

    def _popup(self, e):
        self._build_menu()
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _set_cutoff(self, key: str, name: str, account: str, value: float):
        self.cfg = set_cutoff(name, value or None, account, key)

    def _toggle_topmost(self):
        self.cfg = update_config(topmost=self.topmost_var.get())
        self.root.attributes("-topmost", self.cfg["topmost"])

    def _open_update(self):
        if self.update:
            webbrowser.open(self.update["url"])

    def quit(self):
        self.root.destroy()

    # -- data ----------------------------------------------------------------

    def refresh(self):
        if self.fetching:
            return
        self.fetching = True
        self.status.config(text="Updating…")

        def work():
            results = fetch_all()
            enforce_cutoffs(results)
            update = check_for_update()
            notify_update(update)
            self.root.after(0, lambda: self._apply(results, update))

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, results: list[ProviderUsage], update: dict | None = None):
        self.fetching = False
        self.last_fetch = time.time()
        previous = {u.name: u for u in self.data}
        for u in results:
            # Keep the last good numbers on transient failures, flagged as stale.
            old = previous.get(u.name)
            if u.error and not u.limits and old and old.limits:
                u.limits, u.plan, u.account = old.limits, old.plan, old.account
        self.data = results
        self.update = update
        self.cfg = self._load_config()  # a reached cutoff was cleared, or another computer changed a setting
        self.render()

    def _tick(self):
        # Poll on the configured interval; redraw countdowns every 30 s in between.
        now = time.time()
        last = max((u.fetched_at for u in self.data), default=self.last_fetch)
        if not self.fetching and now - last >= self.interval:
            self.refresh()
        elif self.data:
            self.render()
        self.root.after(30_000 if self.last_fetch else 1_000, self._tick)

    # -- drawing -------------------------------------------------------------

    def render(self):
        tk = self.tk
        for child in self.body.winfo_children():
            child.destroy()

        last = max((u.fetched_at for u in self.data), default=0)
        self.status.config(text="Updated " + time.strftime("%H:%M", time.localtime(last)) if last else "")
        if self.update:
            self.update_link.config(text=f"⬆ v{self.update['version']}")
            self.update_link.pack(side="left", padx=(8, 0), after=self.status)
        else:
            self.update_link.pack_forget()

        for i, u in enumerate(self.data):
            panel = tk.Frame(self.body, bg=THEME["panel"], padx=10, pady=8)
            panel.pack(fill="x", pady=(6 if i else 2, 0))

            head = tk.Frame(panel, bg=THEME["panel"])
            head.pack(fill="x")
            tk.Label(head, text="●", fg=THEME[u.name.lower()] if u.name.lower() in THEME else THEME["fg"],
                     bg=THEME["panel"], font=self.f_label).pack(side="left")
            tk.Label(head, text=u.name, fg=THEME["fg"], bg=THEME["panel"], font=self.f_title).pack(side="left", padx=(4, 0))
            if u.plan:
                tk.Label(head, text=u.plan.capitalize(), fg=THEME["muted"], bg=THEME["panel"],
                         font=self.f_small).pack(side="right")

            if u.error:
                tk.Label(panel, text=("⚠ stale — " if u.limits else "⚠ ") + u.error, fg=THEME["error"],
                         bg=THEME["panel"], font=self.f_small, wraplength=BAR_WIDTH + 40,
                         justify="left").pack(anchor="w", pady=(4, 0))

            for lim in u.limits:
                self._draw_limit(panel, lim)

        self._bind_drag(self.body)

    def _draw_limit(self, parent, lim: Limit):
        tk = self.tk
        row = tk.Frame(parent, bg=THEME["panel"])
        row.pack(fill="x", pady=(6, 0))
        top = tk.Frame(row, bg=THEME["panel"])
        top.pack(fill="x")
        tk.Label(top, text=lim.label, fg=THEME["fg"], bg=THEME["panel"], font=self.f_label).pack(side="left")
        if lim.percent is None:
            self._draw_reset(row, lim)
            return
        color = THEME[lim.level()]
        tk.Label(top, text=f"{lim.percent:.0f}%", fg=color if lim.level() != "normal" else THEME["fg"],
                 bg=THEME["panel"], font=self.f_label).pack(side="right")

        bar = tk.Canvas(row, width=BAR_WIDTH, height=BAR_HEIGHT, bg=THEME["panel"], highlightthickness=0, bd=0)
        bar.pack(fill="x", pady=(2, 0))
        bar.create_rectangle(0, 0, BAR_WIDTH, BAR_HEIGHT, fill=THEME["track"], width=0)
        filled = max(0, min(100, lim.percent)) / 100 * BAR_WIDTH
        if filled:
            bar.create_rectangle(0, 0, filled, BAR_HEIGHT, fill=color, width=0)
        self._draw_reset(row, lim)

    def _draw_reset(self, row, lim: Limit):
        tk = self.tk
        reset = format_remaining(lim.resets_at)
        if reset:
            local = lim.resets_at.astimezone().strftime("%a %H:%M")
            tk.Label(row, text=f"resets in {reset}  ·  {local}", fg=THEME["muted"], bg=THEME["panel"],
                     font=self.f_small).pack(anchor="w")

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------
# Linux top-bar indicator (AppIndicator / StatusNotifierItem)
# --------------------------------------------------------------------------

USAGE_PAGES = {
    "Claude": "https://claude.ai/settings/usage",
    "Codex": "https://chatgpt.com/settings/usage?tab=overview",
    "Bedrock": "https://console.aws.amazon.com/costmanagement/home#/cost-explorer",
    "Vertex": "https://console.cloud.google.com/billing/reports",
    "Azure": "https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/costanalysis",
}
CLOUD_PROVIDERS = {"Bedrock", "Vertex", "Azure"}  # only listed when in use


def usage_pages(data: list[ProviderUsage]) -> dict[str, str]:
    shown = {u.name for u in data}
    return {name: url for name, url in USAGE_PAGES.items() if name in shown or (not data and name not in CLOUD_PROVIDERS)}

# Three rising bars, drawn white like the other top-bar icons; tinted when a limit runs high.
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
<rect x="3" y="12" width="4" height="7" rx="1" fill="{c1}"/>
<rect x="9" y="8" width="4" height="11" rx="1" fill="{c2}"/>
<rect x="15" y="3" width="4" height="16" rx="1" fill="{c3}"/>
</svg>"""
ICON_COLORS = {
    "normal": ("#ffffff", "#ffffff", "#ffffff"),
    "warning": ("#ffffff", "#ffffff", THEME["warning"]),
    "critical": ("#ffffff", THEME["critical"], THEME["critical"]),
    "error": ("#ffffff", "#ffffff80", "#ffffff40"),
}
# "Collapse to logo" in the menu shows this instead of the numbers: the Aleph Void mark, ℵ₀, as a
# single-weight line glyph like the system's own tray icons. assets/llm-usage-widget.svg is the same
# glyph on the app's violet tile.
LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
<g fill="none" stroke="#ffffff" stroke-linecap="round">
<path stroke-width="2.4" d="M4 3.5 C7.8 8.8 11.8 14 15.5 19"/>
<path stroke-width="2.4" d="M8.6 9.8 C5.8 11.6 4.2 14.8 4.4 19.5"/>
<path stroke-width="2.4" d="M11.4 13.4 C15.6 12.6 17.6 9 16.6 3.6"/>
<circle stroke-width="1.4" cx="19.6" cy="20.2" r="1.6"/>
</g>
</svg>"""
COLLAPSE_LABEL = "Collapse to logo"


def load_appindicator():
    """Return the AppIndicator module, or None if PyGObject/libappindicator are missing."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
    except (ImportError, ValueError):
        return None
    for name in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(name, "0.1")
            return __import__("gi.repository", fromlist=[name]).__dict__[name]
        except (ImportError, ValueError, KeyError):
            continue
    return None


class Tray:
    def __init__(self, interval: int, appindicator):
        from gi.repository import GLib, Gtk

        self.GLib, self.Gtk, self.AI = GLib, Gtk, appindicator
        self.interval = max(MIN_INTERVAL, interval)
        self.data: list[ProviderUsage] = []
        self.update: dict | None = None
        self.fetching = False
        self.last_fetch = 0.0

        # Icons are referenced by name from our own theme dir so that switching
        # between them actually repaints (AppIndicator caches by path).
        self.icon_dir = config_dir() / "icons"
        self.icon_dir.mkdir(parents=True, exist_ok=True)
        for level, (c1, c2, c3) in ICON_COLORS.items():
            (self.icon_dir / f"llm-usage-{level}.svg").write_text(ICON_SVG.format(c1=c1, c2=c2, c3=c3))
        (self.icon_dir / "llm-usage-logo.svg").write_text(LOGO_SVG)

        AI = appindicator
        self.ind = AI.Indicator.new(APP_NAME, "llm-usage-normal", AI.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_icon_theme_path(str(self.icon_dir))
        self.ind.set_status(AI.IndicatorStatus.ACTIVE)
        self.ind.set_title("LLM Usage")
        self.ind.set_label("…", "")
        self.render()

        self.refresh()
        GLib.timeout_add_seconds(30, self._tick)

    def refresh(self, *_):
        if self.fetching:
            return
        self.fetching = True

        def work():
            results = fetch_all()
            enforce_cutoffs(results)
            update = check_for_update()
            notify_update(update)
            self.GLib.idle_add(self._apply, results, update)

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, results: list[ProviderUsage], update: dict | None = None):
        self.fetching = False
        self.last_fetch = time.time()
        previous = {u.name: u for u in self.data}
        for u in results:
            old = previous.get(u.name)
            if u.error and not u.limits and old and old.limits:
                u.limits, u.plan, u.account = old.limits, old.plan, old.account
        self.data = results
        self.update = update
        self.render()
        return False

    def _set_cutoff(self, _item, key: str, name: str, account: str, value: float):
        set_cutoff(name, value or None, account, key)
        self.GLib.idle_add(self.render)  # not from inside the menu's own signal handler

    def _toggle_collapsed(self, _item):
        update_config(collapsed=not load_config().get("collapsed", False))
        self.GLib.idle_add(self.render)

    def _tick(self):
        if not self.fetching and time.time() - self.last_fetch >= self.interval:
            self.refresh()
        else:
            self.render()  # keep the "resets in" countdowns current
        return True

    def render(self):
        Gtk = self.Gtk
        # Top bar: the fullest limit of each provider, e.g. "Claude 99%  Codex 13%".
        parts, worst = [], "normal"
        rank = {"normal": 0, "warning": 1, "critical": 2}
        for u in self.data:
            top = top_limit(u)
            if top:
                parts.append(f"{u.name} {top.percent:.0f}%")
                if rank[top.level()] > rank[worst]:
                    worst = top.level()
            elif u.summary:
                parts.append(f"{u.name} {u.summary}")
            elif u.error:
                parts.append(f"{u.name} –")
        if self.data and not any(u.limits for u in self.data):
            worst = "error"
        cfg = load_config()
        if cfg.get("collapsed", False):
            self.ind.set_label("", "")
            self.ind.set_icon_full("llm-usage-logo", "LLM usage")
        else:
            self.ind.set_label("  ".join(parts) if parts else "…", "")
            self.ind.set_icon_full(f"llm-usage-{worst}", "LLM usage")
        # Some top bars show the title as the icon's tooltip.
        self.ind.set_title("LLM Usage" + (f" — {update_label(self.update)}" if self.update else ""))

        menu = Gtk.Menu()

        def info(text):
            item = Gtk.MenuItem(label=text)
            item.set_sensitive(False)
            menu.append(item)

        if self.update:
            item = Gtk.MenuItem(label=update_label(self.update))
            item.connect("activate", lambda _w, u=self.update["url"]: Gtk.show_uri_on_window(
                None, u, Gtk.get_current_event_time()))
            menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())
        if not self.data:
            info("Loading…")
        for i, u in enumerate(self.data):
            if i:
                menu.append(Gtk.SeparatorMenuItem())
            info(f"{u.name}" + (f"  ({u.plan.capitalize()})" if u.plan else ""))
            if u.error:
                info(("⚠ stale — " if u.limits else "⚠ ") + u.error)
            for lim in u.limits:
                if lim.percent is None:
                    reset = format_remaining(lim.resets_at)
                    info(f"    {lim.label}" + (f"  ·  resets in {reset}" if reset else ""))
                    continue
                filled = round(max(0, min(100, lim.percent)) / 10)
                bar = "█" * filled + "░" * (10 - filled)
                mark = {"warning": "  ▲", "critical": "  ●"}.get(lim.level(), "")
                info(f"    {lim.label}:  {lim.percent:.0f}%{mark}")
                reset = format_remaining(lim.resets_at)
                tail = f"  resets in {reset}" if reset else ""
                info(f"    {bar}{tail}")

        menu.append(Gtk.SeparatorMenuItem())
        if self.last_fetch:
            info("Updated " + time.strftime("%H:%M", time.localtime(self.last_fetch)))
        refresh = Gtk.MenuItem(label="Refresh now")
        refresh.connect("activate", self.refresh)
        menu.append(refresh)
        for name, url in usage_pages(self.data).items():
            item = Gtk.MenuItem(label=f"Open {name} usage page")
            item.connect("activate", lambda _w, u=url: Gtk.show_uri_on_window(None, u, Gtk.get_current_event_time()))
            menu.append(item)
        for key, name, account, label, choices in cutoff_menu_entries(cfg, self.data):
            cutoff = cutoff_for(cfg, name, account, key) or 0
            sub = Gtk.Menu()
            for value, text in choices:
                choice = Gtk.CheckMenuItem(label=text)
                choice.set_draw_as_radio(True)
                choice.set_active(value == cutoff)
                choice.connect("activate", self._set_cutoff, key, name, account, value)
                sub.append(choice)
            item = Gtk.MenuItem(label=label)
            item.set_submenu(sub)
            menu.append(item)
        collapse = Gtk.CheckMenuItem(label=COLLAPSE_LABEL)
        collapse.set_active(cfg.get("collapsed", False))
        collapse.connect("activate", self._toggle_collapsed)
        menu.append(collapse)
        about = Gtk.Menu()
        version = Gtk.MenuItem(label=f"LLM Usage Widget — Version {__version__}")
        version.set_sensitive(False)
        about.append(version)
        for text, url in ((f"Source: {REPO_URL}", REPO_URL), (f"Aleph Void LLC: {LLC_URL}", LLC_URL)):
            item = Gtk.MenuItem(label=text)
            item.connect("activate", lambda _w, u=url: Gtk.show_uri_on_window(None, u, Gtk.get_current_event_time()))
            about.append(item)
        about_item = Gtk.MenuItem(label="About")
        about_item.set_submenu(about)
        menu.append(about_item)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(quit_item)
        menu.show_all()
        self.ind.set_menu(menu)

    def run(self):
        import signal
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        self.Gtk.main()


# --------------------------------------------------------------------------
# Windows notification area / macOS menu bar (pystray + Pillow)
# --------------------------------------------------------------------------

def top_limit(u: ProviderUsage) -> Limit | None:
    """The fullest limit that has a percentage."""
    bars = [lim for lim in u.limits if lim.percent is not None]
    return max(bars, key=lambda l: l.percent) if bars else None


def summary_text(data: list[ProviderUsage]) -> str:
    """e.g. "Claude 99%  Codex 13%" — the fullest limit of each provider."""
    parts = []
    for u in data:
        top = top_limit(u)
        if top:
            parts.append(f"{u.name} {top.percent:.0f}%")
        elif u.summary:
            parts.append(f"{u.name} {u.summary}")
        elif u.error:
            parts.append(f"{u.name} –")
    return "  ".join(parts)


def gauge_image(data: list[ProviderUsage], size: int = 64):
    """One vertical gauge per provider, filled to its fullest limit.

    Windows tray icons cannot show text, so the icon itself carries the numbers.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    names = [u.name for u in data] or ["Claude", "Codex"]
    by_name = {u.name: u for u in data}
    gap = size // 10
    width = (size - gap * (len(names) + 1)) // len(names)
    for i, name in enumerate(names):
        x0 = gap + i * (width + gap)
        x1 = x0 + width
        draw.rounded_rectangle((x0, gap, x1, size - gap), radius=size // 12, fill=THEME["track"])
        top = top_limit(by_name[name]) if name in by_name else None
        if top and top.percent > 0:
            height = (size - 2 * gap) * min(100, top.percent) / 100
            draw.rectangle((x0, size - gap - height, x1, size - gap), fill=THEME[top.level()])
    return img


def logo_image(size: int = 64, color: str = "#ffffff"):
    """LOGO_SVG drawn with Pillow in one color: three round-capped cubic strokes and a ring."""
    import xml.etree.ElementTree as ET
    from PIL import Image, ImageDraw

    supersample = 8  # Pillow's shapes aren't anti-aliased; draw big and scale down
    big = size * supersample
    s = big / 24  # the SVG is drawn on a 24 grid
    mask = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(mask)
    for el in ET.fromstring(LOGO_SVG).iter():
        tag, a = el.tag.split("}")[1], el.attrib
        if tag == "path":  # "M x y C x y x y x y"
            x0, y0, x1, y1, x2, y2, x3, y3 = (float(n) * s for n in re.findall(r"[\d.]+", a["d"]))
            pts = [((1 - t) ** 3 * x0 + 3 * (1 - t) ** 2 * t * x1 + 3 * (1 - t) * t * t * x2 + t ** 3 * x3,
                    (1 - t) ** 3 * y0 + 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t * t * y2 + t ** 3 * y3)
                   for t in (i / 48 for i in range(49))]
            w = float(a["stroke-width"]) * s
            d.line(pts, fill=255, width=round(w), joint="curve")
            for x, y in (pts[0], pts[-1]):  # round caps
                d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=255)
        elif tag == "circle":
            cx, cy, r, w = (float(a[k]) * s for k in ("cx", "cy", "r", "stroke-width"))
            d.ellipse([cx - r - w / 2, cy - r - w / 2, cx + r + w / 2, cy + r + w / 2], outline=255, width=round(w))
    img = Image.new("RGBA", (size, size), color)
    img.putalpha(mask.resize((size, size), Image.LANCZOS))
    return img


def app_icon_image(size: int = 1024):
    """The app icon: the logo in white on a violet rounded tile, as in assets/llm-usage-widget.svg."""
    from PIL import Image, ImageDraw

    supersample = 4
    big = size * supersample
    s = big / 256  # the app SVG is drawn on a 256 grid
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([12 * s, 12 * s, 244 * s, 244 * s], radius=52 * s, fill="#6d28d9")
    img = img.resize((size, size), Image.LANCZOS)
    glyph = logo_image(round(size * 168 / 256))  # the SVG's scale(7) of the 24 grid
    offset = (size - glyph.width) // 2
    img.alpha_composite(glyph, (offset, offset))
    return img


def taskbar_is_light() -> bool:
    """Windows: whether the taskbar uses the light theme, where a white icon would vanish."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


class TaskbarStrip:
    """Windows: "Claude 99%  Codex 16%" on the taskbar, just left of the notification area.

    Windows has no API for putting text in the taskbar, so this is a small borderless
    always-on-top window kept over the taskbar's empty space: next to the tray when there
    is room, else in another gap (found with UI Automation), on two stacked lines if that
    is what fits. It follows the taskbar's theme and layout, steps aside for full-screen
    apps and an auto-hidden taskbar, and opens the tray menu when clicked. With "Collapse to
    logo" it shows the logo instead of the numbers, so the app stays on the taskbar even when
    Windows has tucked the tray icon into the overflow menu. Runs on the main thread.
    """

    LOGO = "logo"  # what _update passes as the parts when collapsed

    TICK_MS = 500  # how often to re-check position and z-order
    LAYOUT_MS = 2000  # how often to re-read where the taskbar's buttons are (a cross-process query)

    def __init__(self, tray: "DesktopTray"):
        import ctypes
        import tkinter as tk
        import tkinter.font as tkfont
        from ctypes import wintypes

        self.tk, self.ctypes, self.wintypes = tk, ctypes, wintypes
        # Private DLL handles with full signatures: without them ctypes passes handles and
        # HWND_TOPMOST (-1) as 32-bit ints, which 64-bit Windows rejects as invalid handles.
        # (Private so they don't clash with the signatures pystray sets on ctypes.windll.)
        W = wintypes
        self.user32 = u = ctypes.WinDLL("user32")
        self.gdi32 = g = ctypes.WinDLL("gdi32")
        for fn, args, res in (
            (u.FindWindowW, [W.LPCWSTR, W.LPCWSTR], W.HWND),
            (u.FindWindowExW, [W.HWND, W.HWND, W.LPCWSTR, W.LPCWSTR], W.HWND),
            (u.GetWindowRect, [W.HWND, ctypes.POINTER(W.RECT)], W.BOOL),
            (u.IsWindowVisible, [W.HWND], W.BOOL),
            (u.GetForegroundWindow, [], W.HWND),
            (u.GetClassNameW, [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int),
            (u.GetWindowThreadProcessId, [W.HWND, ctypes.POINTER(W.DWORD)], W.DWORD),
            (u.SetForegroundWindow, [W.HWND], W.BOOL),
            (u.AttachThreadInput, [W.DWORD, W.DWORD, W.BOOL], W.BOOL),
            (u.GetSystemMetrics, [ctypes.c_int], ctypes.c_int),
            (u.GetDC, [W.HWND], W.HDC),
            (u.ReleaseDC, [W.HWND, W.HDC], ctypes.c_int),
            (u.SetWindowPos, [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT],
             W.BOOL),
            (g.GetPixel, [W.HDC, ctypes.c_int, ctypes.c_int], W.DWORD),
        ):
            fn.argtypes, fn.restype = args, res
        self.tray = tray
        self.closing = threading.Event()
        self.shown_key = None  # what is drawn now, to redraw only on change
        self.placed = None
        self.uia = self._init_uia()
        self.spans, self.spans_at = None, 0.0

        root = self.root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)  # no frame, no taskbar button
        root.attributes("-topmost", True)
        # One line when there is room; two smaller stacked lines when the taskbar is crowded.
        self.fonts = {
            "line": (tkfont.Font(family="Segoe UI", size=9), tkfont.Font(family="Segoe UI", size=9, weight="bold")),
            "stacked": (tkfont.Font(family="Segoe UI", size=8),
                        tkfont.Font(family="Segoe UI", size=8, weight="bold")),
        }
        self.pad = round(4 * float(root.tk.call("tk", "scaling")))
        self.bg = "#1c1c1c"
        self.frame = tk.Frame(root, bg=self.bg)
        self.frame.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.frame, bg=self.bg)
        self.inner.place(relx=0.5, rely=0.5, anchor="center")
        root.update_idletasks()
        self.hwnd = int(root.wm_frame(), 16)
        self._tick()

    @staticmethod
    def _init_uia():
        """UI Automation, which knows where the taskbar's buttons really are; None if unavailable."""
        try:
            import comtypes.client
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIA
            return UIA, comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        except Exception:
            return None

    # -- taskbar geometry ----------------------------------------------------

    def _rect(self, hwnd):
        r = self.wintypes.RECT()
        if not hwnd or not self.user32.GetWindowRect(hwnd, self.ctypes.byref(r)):
            return None
        return r.left, r.top, r.right, r.bottom

    def _placement(self):
        """(taskbar window, taskbar rect, tray's left edge), or None when the strip should be hidden."""
        u = self.user32
        taskbar = u.FindWindowW("Shell_TrayWnd", None)
        tb = self._rect(taskbar)
        if not tb or not u.IsWindowVisible(taskbar):
            return None
        left, top, right, bottom = tb
        if bottom - top > right - left:
            return None  # vertical taskbar (Windows 10): no room for a line of text
        screen_h = u.GetSystemMetrics(1)  # SM_CYSCREEN
        if top >= screen_h - 4 or bottom <= 4:
            return None  # auto-hidden
        if self._fullscreen_app(taskbar):
            return None
        tray = self._rect(u.FindWindowExW(taskbar, None, "TrayNotifyWnd", None))
        return taskbar, tb, tray[0] if tray else right - 300

    # Legacy windows whose rectangles no longer match the Windows 11 taskbar's real layout.
    LEGACY_CLASSES = ("MSTaskSwWClass", "MSTaskListWClass", "ReBarWindow32", "TrayNotifyWnd")

    def _free_spans(self, taskbar, tb, tray_left) -> list[tuple[int, int]]:
        """Empty stretches of the taskbar left of the tray, rightmost first.

        Taskbar buttons are centered by default on Windows 11 and can reach the tray when
        many apps are open, so the strip must not assume the space next to the tray is free.
        """
        left, top, right, bottom = tb
        busy = []
        if self.uia:
            UIA, automation = self.uia
            try:
                found = automation.ElementFromHandle(taskbar).FindAll(
                    UIA.TreeScope_Descendants, automation.CreateTrueCondition())
                for i in range(found.Length):
                    el = found.GetElement(i)
                    r = el.CurrentBoundingRectangle
                    if (r.left < tray_left and 2 * (r.bottom - r.top) > bottom - top
                            and 0 < r.right - r.left < 0.9 * (right - left)
                            and el.CurrentClassName not in self.LEGACY_CLASSES):
                        busy.append((r.left, r.right))  # a button, the search box, the widgets...
            except Exception:
                busy = []
        if not busy:
            return [(left, tray_left)]  # unknown layout: use the space next to the tray
        spans, x = [], left
        for a, b in sorted(busy):
            if a > x:
                spans.append((x, a))
            x = max(x, b)
        if tray_left > x:
            spans.append((x, tray_left))
        return spans[::-1]

    def _is_shell(self, hwnd, taskbar) -> bool:
        """Whether hwnd belongs to the process that owns the taskbar (Explorer)."""
        pid, shell_pid = self.ctypes.c_ulong(), self.ctypes.c_ulong()
        self.user32.GetWindowThreadProcessId(hwnd, pid)
        self.user32.GetWindowThreadProcessId(taskbar, shell_pid)
        return bool(pid.value) and pid.value == shell_pid.value

    def take_focus_from_taskbar(self) -> bool:
        """If the taskbar has the focus, move it to the strip; True if it did (any thread).

        Windows 11 lifts a focused taskbar into a z-order band that nothing else can be above,
        hiding the strip behind it until some ordinary window is activated. Only the foreground
        process may change the foreground window, so share the input queue of the shell's
        thread for the call, the usual way around that.
        """
        u = self.user32
        fg, taskbar = u.GetForegroundWindow(), u.FindWindowW("Shell_TrayWnd", None)
        if not (fg and taskbar and self._is_shell(fg, taskbar)):
            return False
        shell_thread, me = u.GetWindowThreadProcessId(fg, None), threading.get_native_id()
        u.AttachThreadInput(me, shell_thread, True)
        try:
            u.SetForegroundWindow(self.hwnd)
        finally:
            u.AttachThreadInput(me, shell_thread, False)
        return True

    def _fullscreen_app(self, taskbar) -> bool:
        u = self.user32
        fg = u.GetForegroundWindow()
        if not fg or fg in (taskbar, self.hwnd):
            return False
        buf = self.ctypes.create_unicode_buffer(64)
        u.GetClassNameW(fg, buf, 64)
        if buf.value in ("Progman", "WorkerW"):
            return False  # the desktop
        if self._is_shell(fg, taskbar):
            # Windows 11 puts the taskbar's flyouts in a screen-sized XamlExplorerHostIslandWindow,
            # the foreground window whenever the taskbar has the focus.
            return False
        r = self._rect(fg)
        return bool(r) and r[0] <= 0 and r[1] <= 0 and r[2] >= u.GetSystemMetrics(0) and r[3] >= u.GetSystemMetrics(1)

    def _taskbar_color(self, x: int, y: int) -> str:
        """The taskbar's own color just outside the strip, so the strip blends in."""
        dc = self.user32.GetDC(None)
        try:
            c = self.gdi32.GetPixel(dc, x, y)
        finally:
            self.user32.ReleaseDC(None, dc)
        if c == 0xFFFFFFFF:  # CLR_INVALID
            return self.bg
        return f"#{c & 0xFF:02x}{(c >> 8) & 0xFF:02x}{(c >> 16) & 0xFF:02x}"

    # -- drawing -------------------------------------------------------------

    def _parts(self) -> list[tuple[str, str, str]]:
        """(provider, value, level) for each provider, e.g. ("Claude", "99%", "critical")."""
        parts = []
        for u in self.tray.data:
            top = top_limit(u)
            if top:
                parts.append((u.name, f"{top.percent:.0f}%", top.level()))
            elif u.summary:
                parts.append((u.name, u.summary, "normal"))
            elif u.error:
                parts.append((u.name, "–", "muted"))
        return parts or [("LLM usage", "…", "muted")]

    def _width(self, parts, layout: str) -> int:
        pad = self.pad
        if parts == self.LOGO:
            return self._logo_size() + 2 * pad
        font, bold = self.fonts[layout]
        if layout == "line":
            return sum(font.measure(n) + pad // 2 + bold.measure(v) for n, v, _ in parts) + 2 * pad * len(parts)
        return max(font.measure(n) for n, _, _ in parts) + pad + max(bold.measure(v) for _, v, _ in parts) + 2 * pad

    def _choose(self, parts, spans, margin):
        """(layout, right edge) of the first layout that fits, preferring the spot next to the tray."""
        for a, b in spans:
            for layout in ("line", "stacked"):
                if b - a >= self._width(parts, layout) + 2 * margin:
                    return layout, b - margin
        return None

    def _logo_size(self) -> int:
        return 4 * self.pad  # 16pt, the size of the tray's own icons

    def _draw(self, bg: str, parts, layout: str):
        key = (bg, tuple(parts), layout)
        if key == self.shown_key:
            return
        self.shown_key, self.bg = key, bg
        tk = self.tk
        r, g, b = (int(bg[i:i + 2], 16) for i in (1, 3, 5))
        light = 0.299 * r + 0.587 * g + 0.114 * b > 140
        fg = "#000000" if light else "#ffffff"
        colors = {"normal": fg, "muted": "#5f6068" if light else THEME["muted"],
                  "warning": "#a86b00" if light else THEME["warning"],
                  "critical": "#c62828" if light else THEME["critical"]}

        for child in self.inner.winfo_children():
            child.destroy()
        self.frame.configure(bg=bg)
        self.inner.configure(bg=bg)
        font, bold = self.fonts[layout]
        pad = self.pad
        if parts == self.LOGO:
            from PIL import ImageTk
            self.photo = ImageTk.PhotoImage(logo_image(self._logo_size(), fg))  # Tk doesn't keep a reference
            tk.Label(self.inner, image=self.photo, bg=bg, padx=pad, pady=0, bd=0).pack()
            parts = []
        for i, (name, value, level) in enumerate(parts):
            name_label = tk.Label(self.inner, text=name, font=font, fg=fg, bg=bg, padx=0, pady=0, bd=0)
            value_label = tk.Label(self.inner, text=value, font=bold, fg=colors[level], bg=bg, padx=0, pady=0, bd=0)
            if layout == "line":
                name_label.pack(side="left", padx=(pad * 2 if i else pad, pad // 2))
                value_label.pack(side="left", padx=(0, pad if i == len(parts) - 1 else 0))
            else:
                name_label.grid(row=i, column=0, sticky="w", padx=(pad, pad))
                value_label.grid(row=i, column=1, sticky="e", padx=(0, pad))
        for w in [self.frame, self.inner, *self.inner.winfo_children()]:
            w.bind("<ButtonRelease-1>", self._click)
            w.bind("<ButtonRelease-3>", self._click)

    def _click(self, _e=None):
        self.tray.open_menu()

    def _tick(self):
        if self.closing.is_set():
            self.root.destroy()
            return
        try:
            self._update()
        except Exception:
            pass  # never let a stray Win32 hiccup stop the loop
        self.root.after(self.TICK_MS, self._tick)

    def _hide(self):
        if self.root.state() != "withdrawn":
            self.root.withdraw()
            self.placed = None

    def _update(self):
        cfg = self.tray.cfg
        place = self._placement() if cfg.get("taskbar_text", True) else None
        if place is None:
            self._hide()
            return
        taskbar, tb, tray_left = place
        top, bottom = tb[1], tb[3]
        now = time.monotonic()
        if self.spans is None or now - self.spans_at >= self.LAYOUT_MS / 1000:
            self.spans, self.spans_at = self._free_spans(taskbar, tb, tray_left), now
        margin = (bottom - top) // 8
        parts = self.LOGO if cfg.get("collapsed", False) else self._parts()
        choice = self._choose(parts, self.spans, margin)
        if choice is None:
            self._hide()  # the taskbar is full; the tray icon still has the numbers
            return
        layout, edge = choice
        # The pixel sampled is in the margin right of the strip, never under it or a button.
        self._draw(self._taskbar_color(edge + margin // 2, top + 2), parts, layout)
        self.root.update_idletasks()
        w = self.inner.winfo_reqwidth()
        geom = (w, bottom - top - 2 * margin, edge - w, top + margin)
        if geom != self.placed:
            self.root.geometry("%dx%d+%d+%d" % geom)
            self.placed = geom
        if self.root.state() == "withdrawn":
            self.root.deiconify()
        # Also place it with Win32: Tk resizes but doesn't always move a mapped borderless
        # window, and clicking the taskbar raises it over other topmost windows.
        HWND_TOPMOST, SWP_NOACTIVATE = -1, 0x0010
        w, h, x, y = geom
        self.user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE)

    def close(self):
        self.closing.set()  # picked up on the Tk thread

    def run(self):
        self.root.mainloop()


class DesktopTray:
    def __init__(self, interval: int):
        import pystray

        self.pystray = pystray
        self.interval = max(MIN_INTERVAL, interval)
        self.data: list[ProviderUsage] = []
        self.update: dict | None = None
        self.last_fetch = 0.0
        self.wake = threading.Event()
        self.cfg = load_config()
        self.strip = None
        self.icon = pystray.Icon(APP_NAME, gauge_image([]), "LLM Usage", menu=self._menu())
        if sys.platform == "win32":
            self._open_menu_on_any_click()
            try:
                self.strip = TaskbarStrip(self)
            except Exception as e:  # e.g. Python built without tkinter
                warn(f"Taskbar text unavailable ({e}); showing only the tray icon.")
        if sys.platform == "darwin":
            # Menu-bar only: no Dock icon or app menu.
            import AppKit
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory)

    def _open_menu_on_any_click(self):
        # pystray opens the menu only on right-click, and rebuilds it on whichever thread
        # sets icon.menu, which can destroy it while it is open. Instead open it on any
        # click, rebuilding it first on the icon's own thread so the countdowns are current.
        from pystray._util import win32

        on_notify = self.icon._message_handlers[win32.WM_NOTIFY]

        def handler(wparam, lparam):
            if lparam in (win32.WM_LBUTTONUP, win32.WM_RBUTTONUP):
                self.icon.menu = self._menu()
                result = on_notify(wparam, win32.WM_RBUTTONUP)  # returns once the menu has closed
                self._unfocus_taskbar()
                return result
            return on_notify(wparam, lparam)

        self.icon._message_handlers[win32.WM_NOTIFY] = handler
        self._win32 = win32

    def _unfocus_taskbar(self):
        """After the menu closes, keep the taskbar from holding the focus, which would hide the strip.

        Dismissing the menu by clicking on the taskbar gives the taskbar the focus (see
        TaskbarStrip.take_focus_from_taskbar), a moment after the menu has gone, so watch for a bit.
        """
        if not self.strip:
            return
        for _ in range(10):
            time.sleep(0.05)
            self.strip.take_focus_from_taskbar()

    def open_menu(self):
        """Show the tray menu at the mouse pointer (Windows; any thread)."""
        hwnd = getattr(self.icon, "_hwnd", None)
        if hwnd:
            self._win32.PostMessage(hwnd, self._win32.WM_NOTIFY, 0, self._win32.WM_LBUTTONUP)

    def _toggle_taskbar_text(self):
        self.cfg = update_config(taskbar_text=not self.cfg.get("taskbar_text", True))

    def _toggle_collapsed(self):
        self.cfg = update_config(collapsed=not self.cfg.get("collapsed", False))
        self._on_main(self._render)

    def _cutoff_action(self, key: str, name: str, account: str, value: float):
        def action():
            self.cfg = set_cutoff(name, value or None, account, key)
            if sys.platform != "win32":  # on Windows the menu is rebuilt when it is opened
                self._on_main(self._render)
        return action

    def _cutoff_checked(self, key: str, name: str, account: str, value: float):
        return lambda _item: (cutoff_for(self.cfg, name, account, key) or 0) == value

    def _quit(self):
        if self.strip:
            self.strip.close()  # run() stops the icon once the strip's loop has ended
        else:
            self.icon.stop()

    def _on_main(self, fn):
        # AppKit objects may only be touched from the main thread.
        if sys.platform == "darwin":
            from PyObjCTools import AppHelper
            AppHelper.callAfter(fn)
        else:
            fn()

    def _menu(self):
        item, sep = self.pystray.MenuItem, self.pystray.Menu.SEPARATOR
        items = []

        def info(text):
            items.append(item(text, None, enabled=False))

        if self.update:
            items.append(item(update_label(self.update), (lambda u: lambda: webbrowser.open(u))(self.update["url"])))
            items.append(sep)
        if not self.data:
            info("Loading…")
        for i, u in enumerate(self.data):
            if i:
                items.append(sep)
            info(u.name + (f"  ({u.plan.capitalize()})" if u.plan else ""))
            if u.error:
                info(("⚠ stale — " if u.limits else "⚠ ") + u.error)
            for lim in u.limits:
                mark = {"warning": "  ▲", "critical": "  ●"}.get(lim.level(), "")
                reset = format_remaining(lim.resets_at)
                value = "" if lim.percent is None else f":  {lim.percent:.0f}%{mark}"
                info(f"    {lim.label}{value}" + (f"   ·  resets in {reset}" if reset else ""))
        items.append(sep)
        if self.last_fetch:
            info("Updated " + time.strftime("%H:%M", time.localtime(self.last_fetch)))
        items.append(item("Refresh now", lambda: self.wake.set()))
        for name, url in usage_pages(self.data).items():
            items.append(item(f"Open {name} usage page", (lambda u: lambda: webbrowser.open(u))(url)))
        for key, name, account, label, choices in cutoff_menu_entries(self.cfg, self.data):
            items.append(item(label, self.pystray.Menu(*(
                item(text, self._cutoff_action(key, name, account, value),
                     checked=self._cutoff_checked(key, name, account, value), radio=True)
                for value, text in choices))))
        if self.strip:
            items.append(item("Show usage on the taskbar", lambda: self._toggle_taskbar_text(),
                              checked=lambda _i: self.cfg.get("taskbar_text", True)))
        items.append(item(COLLAPSE_LABEL, lambda: self._toggle_collapsed(),
                          checked=lambda _i: self.cfg.get("collapsed", False)))
        items.append(item("About", self.pystray.Menu(
            item(f"LLM Usage Widget — Version {__version__}", None, enabled=False),
            item(f"Source: {REPO_URL}", lambda: webbrowser.open(REPO_URL)),
            item(f"Aleph Void LLC: {LLC_URL}", lambda: webbrowser.open(LLC_URL)),
        )))
        items.append(item("Quit", lambda: self._quit()))
        return self.pystray.Menu(*items)

    def _render(self):
        text = summary_text(self.data)
        collapsed = self.cfg.get("collapsed", False)
        if collapsed:
            # Black on macOS, where it is marked as a template image below so the menu bar tints it.
            dark = sys.platform == "darwin" or taskbar_is_light()
            self.icon.icon = logo_image(color="#000000" if dark else "#ffffff")
        else:
            self.icon.icon = gauge_image(self.data)
        tip = text or "LLM Usage"
        if self.update:
            tip += "\n" + update_label(self.update)
        self.icon.title = tip  # tooltip
        if sys.platform != "win32":  # on Windows the menu is rebuilt when it is opened
            self.icon.menu = self._menu()
        if sys.platform == "darwin":
            # Show the summary next to the icon, like the Linux top bar.
            try:
                button = self.icon._status_item.button()
                button.setTitle_(" " + text if text and not collapsed else "")
                button.setImagePosition_(2)  # NSImageLeft
                button.image().setTemplate_(collapsed)
            except Exception:
                pass

    def _apply(self, results: list[ProviderUsage], update: dict | None = None):
        previous = {u.name: u for u in self.data}
        for u in results:
            old = previous.get(u.name)
            if u.error and not u.limits and old and old.limits:
                u.limits, u.plan, u.account = old.limits, old.plan, old.account
        self.data = results
        self.update = update
        self.cfg = load_config()  # a reached cutoff was cleared, or another computer changed a setting
        self._render()

    def _loop(self, icon):
        icon.visible = True
        while True:
            if self.wake.is_set() or time.time() - self.last_fetch >= self.interval:
                self.wake.clear()
                results = fetch_all()
                enforce_cutoffs(results)
                self.last_fetch = time.time()
                update = check_for_update()
                # pystray can show a notification itself on Windows; elsewhere notify_update has its own way.
                notify_update(update, self.icon.notify if getattr(self.icon, "HAS_NOTIFICATION", False)
                              and sys.platform == "win32" else None)
                self._on_main(lambda r=results, u=update: self._apply(r, u))
            else:
                self._on_main(self._render)  # keep the "resets in" countdowns current
            self.wake.wait(30)

    def run(self):
        setup = lambda icon: threading.Thread(target=self._loop, args=(icon,), daemon=True).start()  # noqa: E731
        if self.strip:
            # Tk needs the main thread, so the icon runs its message loop on its own thread.
            self.icon.run_detached(setup)
            self.strip.run()
            self.icon.stop()
        else:
            self.icon.run(setup=setup)


def warn(msg: str) -> None:
    # Windowed builds (pythonw, PyInstaller --windowed) have no stderr.
    if sys.stderr:
        print(msg, file=sys.stderr)


def acquire_single_instance():
    """Hold a lock for the life of the process; None if another copy has it."""
    path = config_dir() / "instance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--once", action="store_true", help="print current usage and exit")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"seconds between refreshes (min {MIN_INTERVAL}, default {DEFAULT_INTERVAL})")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--tray", action="store_true", help="show in the top bar / menu bar / notification area (the default)")
    mode.add_argument("--widget", action="store_true", help="show the floating widget window")
    ap.add_argument("--framed", action="store_true",
                    help="use a normal window with title bar (if your window manager mishandles borderless windows)")
    args = ap.parse_args()
    if args.once:
        return print_once()

    lock = acquire_single_instance()  # noqa: F841 — held until the process exits
    if lock is None:
        warn("LLM Usage Widget is already running.")
        return 0

    if sys.platform.startswith("linux") and not args.widget:
        appindicator = load_appindicator()
        if appindicator:
            Tray(args.interval, appindicator).run()
            return 0
        msg = ("Top-bar mode needs PyGObject and libayatana-appindicator "
               "(Debian/Ubuntu: sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1). "
               "If they are installed, run this with the system Python: /usr/bin/python3")
        if args.tray:
            warn(msg)
            return 1
        warn(msg + "\nFalling back to the floating widget.")
    elif sys.platform in ("win32", "darwin") and not args.widget:
        try:
            import PIL  # noqa: F401
            import pystray  # noqa: F401
        except ImportError:
            msg = "Tray mode needs pystray and Pillow: python3 -m pip install -r requirements.txt"
            if args.tray:
                warn(msg)
                return 1
            warn(msg + "\nFalling back to the floating widget.")
        else:
            if sys.platform == "win32":
                # Crisp tray icon and taskbar text on scaled displays; must precede any window.
                try:
                    import ctypes
                    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
                except (AttributeError, OSError):
                    pass
            DesktopTray(args.interval).run()
            return 0

    Widget(args.interval, args.framed).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
