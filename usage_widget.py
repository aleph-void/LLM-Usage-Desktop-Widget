#!/usr/bin/env python3
"""Desktop widget showing Claude and Codex (ChatGPT) plan usage limits.

Reads the OAuth logins that Claude Code and the Codex CLI already store on
this machine, so there is nothing to configure: if `claude` and `codex` are
logged in, the widget works. Credentials are only ever read, never written,
and are sent only to the provider that issued them.

When Claude Code or Codex run on Amazon Bedrock (including the bedrock-mantle
endpoint) there are no plan limits; instead it shows Bedrock spend and token
counts using your AWS credentials (needs boto3).

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
import os
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "0.0.2"

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

    def level(self) -> str:
        if self.severity in ("warning", "critical"):
            return self.severity
        if self.percent is None:
            return "normal"
        if self.percent >= 90:
            return "critical"
        if self.percent >= 75:
            return "warning"
        return "normal"


@dataclass
class ProviderUsage:
    name: str
    plan: str = ""
    limits: list[Limit] = field(default_factory=list)
    error: str = ""
    fetched_at: float = 0.0
    summary: str = ""  # top-bar text when no limit has a percentage, e.g. "$12.34"


class AuthError(Exception):
    """Login missing or expired; the user has to act in the provider's CLI."""


class NotUsed(Exception):
    """The CLI is set up for another backend (e.g. Bedrock); leave this provider out."""


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


def http_json(url: str, headers: dict[str, str], timeout: float = 20) -> dict:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = _ssl_context()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(f"HTTP {e.code}: login rejected") from e
        raise RuntimeError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network: {e.reason}") from e


def parse_time(value) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError):
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


# On macOS and Windows the settings sync between computers through iCloud Drive or
# OneDrive: config.json in the cloud folder holds everything but these keys, which
# describe this computer. The local config.json keeps a full copy, used whenever the
# cloud folder is unavailable. "sync": false in the local file turns syncing off.
LOCAL_ONLY_KEYS = ("x", "y", "taskbar_text", "sync", "cutoff_handled")


def cloud_config_dir() -> Path | None:
    """The iCloud Drive (macOS) or OneDrive (Windows) folder for synced settings; None if there is none."""
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    elif sys.platform == "win32":
        root = next((Path(os.environ[v]) for v in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
                     if os.environ.get(v)), None)
    else:
        return None
    return root / APP_NAME if root is not None and root.is_dir() else None


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    # Replace in one step so a sync client never uploads a half-written file.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _synced_path(local: dict) -> Path | None:
    if local.get("sync", True) is False:
        return None
    folder = cloud_config_dir()
    if folder is None or (folder / ".config.json.icloud").exists():
        return None  # no cloud folder, or iCloud hasn't downloaded the file: don't overwrite it
    return folder / "config.json"


def _shared(cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if k not in LOCAL_ONLY_KEYS}


def load_config() -> dict:
    local = _read_json(config_dir() / "config.json") or {}
    path = _synced_path(local)
    if path is None:
        return local
    shared = _read_json(path)
    if shared is None:
        if path.exists():
            return local  # unreadable, e.g. mid-sync: keep going on the local copy
        shared = _shared(local)  # first run with this cloud folder: start from this computer's settings
        if not shared:
            return local  # nothing to share yet; save_config creates the file
        try:
            _write_json(path, shared)
        except OSError:
            return local
    return {**shared, **{k: local[k] for k in LOCAL_ONLY_KEYS if k in local}}


def save_config(cfg: dict) -> None:
    try:
        _write_json(config_dir() / "config.json", cfg)
    except OSError:
        pass
    path = _synced_path(cfg)
    shared = _shared(cfg)
    # Don't touch the cloud for a window move, or create the folder with nothing to sync.
    if path is not None and _read_json(path) != shared and (shared or path.exists()):
        try:
            _write_json(path, shared)
        except OSError:
            pass


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
    if claude_uses_bedrock():
        raise NotUsed
    raise AuthError("not logged in — run `claude` and /login")


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


def fetch_claude() -> ProviderUsage:
    creds = _claude_credentials()
    expires = creds.get("expiresAt")
    if expires and expires / 1000 < time.time():
        raise AuthError("login expired — open Claude Code to refresh it")
    data = http_json(
        "https://api.anthropic.com/api/oauth/usage",
        {"Authorization": f"Bearer {creds['accessToken']}", "anthropic-beta": "oauth-2025-04-20"},
    )
    usage = ProviderUsage("Claude", plan=str(creds.get("subscriptionType") or ""))

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
    if extra.get("is_enabled") and extra.get("used_credits"):
        cap = extra.get("monthly_limit")
        pct = float(extra.get("utilization") or 0)
        label = f"Extra usage ${extra['used_credits']:.2f}" + (f" / ${cap:.0f}" if cap else "")
        usage.limits.append(Limit(label, pct))
    return usage


# --------------------------------------------------------------------------
# Codex (ChatGPT subscription via the Codex CLI's login)
# --------------------------------------------------------------------------

def _codex_tokens() -> dict:
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if codex_bedrock_urls():
            raise NotUsed from None
        raise AuthError("not logged in — run `codex login`") from None
    except (OSError, json.JSONDecodeError) as e:
        raise AuthError(f"cannot read {path}: {e}") from None
    tokens = data.get("tokens") or {}
    if not tokens.get("access_token"):
        if codex_bedrock_urls():
            raise NotUsed
        if data.get("OPENAI_API_KEY"):
            raise AuthError("API-key login has no plan limits — use `codex login` with ChatGPT")
        raise AuthError("not logged in — run `codex login`")
    return tokens


def codex_bedrock_urls() -> list[str]:
    """base_url values in Codex's config.toml that point at Bedrock (runtime or bedrock-mantle)."""
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return re.findall(r"""^\s*base_url\s*=\s*["']([^"']*bedrock[^"']*)["']""", text, re.MULTILINE)


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
    usage = ProviderUsage("Codex", plan=str(data.get("plan_type") or ""))
    _codex_windows("", data.get("rate_limit"), usage.limits)
    _codex_windows("Code review", data.get("code_review_rate_limit"), usage.limits)
    extra = data.get("additional_rate_limits")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, dict):
                name = item.get("limit_name") or item.get("metered_feature") or item.get("name") or "Other"
                _codex_windows(str(name), item.get("rate_limit", item), usage.limits)
    credits = data.get("credits") or {}
    if credits.get("has_credits") and not credits.get("unlimited"):
        usage.limits.append(Limit(f"Credits: {credits.get('balance', '0')}", 0))
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


def _cached(key: str, max_age: float, compute):
    """compute() at most once per max_age seconds, remembered in a file across restarts."""
    path = config_dir() / "bedrock-cache.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}
    now = time.time()
    entry = cache.get(key)
    if isinstance(entry, dict) and 0 <= now - entry.get("at", 0) < max_age:
        return entry["value"]
    value = compute()
    cache = {k: v for k, v in cache.items() if isinstance(v, dict) and now - v.get("at", 0) < 7 * 86400}
    cache[key] = {"at": now, "value": value}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
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
        request["NextPageToken"] = resp["NextPageToken"]


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
    for page in pages:
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


PROVIDERS = (("Claude", fetch_claude), ("Codex", fetch_codex), ("Bedrock", fetch_bedrock))


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
# Bedrock for Bedrock), then clears that cutoff so the CLIs can be started again.
# With synced settings every computer stops its own CLIs. Set from the menu, or in
# config.json:
#
#     "cutoff": {"Claude": 95, "Codex": 90, "Bedrock": 100}

CUTOFF_CHOICES = (80, 90, 95, 99)
CUTOFF_SYNC_WINDOW = 3600  # how long other computers act on a cutoff reached on one of them
# npm-installed CLIs run as `node <script>`; these mark the script's package.
CLI_PACKAGES = {"claude": "@anthropic-ai/claude-code/", "codex": "@openai/codex/"}
SCRIPT_RUNTIMES = {"node", "bun", "deno"}
# The Claude and Codex desktop apps have executables of the same names; leave them alone.
DESKTOP_APP_PATHS = (".app/contents/", "/anthropicclaude/", "/windowsapps/")


def cutoff_for(cfg: dict, name: str) -> float | None:
    """The cutoff percentage set for this provider, or None if it has none."""
    cutoffs = cfg.get("cutoff")
    value = cutoffs.get(name) if isinstance(cutoffs, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 < value <= 100 else None


def _dict_setting(cfg: dict, key: str) -> dict:
    value = cfg.get(key)
    return dict(value) if isinstance(value, dict) else {}


def set_cutoff(name: str, value: int | None) -> dict:
    """Save a provider's cutoff (None turns it off); returns the updated config."""
    cfg = load_config()  # re-read so hand edits and other computers' changes are kept
    cutoffs = _dict_setting(cfg, "cutoff")
    if value:
        cutoffs[name] = value
    else:
        cutoffs.pop(name, None)
    if cutoffs:
        cfg["cutoff"] = cutoffs
    else:
        cfg.pop("cutoff", None)
    save_config(cfg)
    return cfg


def cutoff_menu_providers(data: list[ProviderUsage]) -> list[str]:
    """Providers whose cutoff the menu offers: those shown, or Claude and Codex before the first fetch."""
    return [u.name for u in data] or ["Claude", "Codex"]


def cutoff_menu_label(cfg: dict, name: str) -> str:
    cutoff = cutoff_for(cfg, name)
    return f"Stop {name} at: " + (f"{cutoff:.0f}%" if cutoff is not None else "off")


def cutoff_clis(name: str) -> set[str]:
    """The CLIs that draw on this provider's quota."""
    if name == "Claude":
        return {"claude"}
    if name == "Codex":
        return {"codex"}
    if name == "Bedrock":
        return ({"claude"} if claude_uses_bedrock() else set()) | ({"codex"} if codex_bedrock_urls() else set())
    return set()


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
    """Stop the CLIs of every provider at or over its cutoff, then clear that cutoff.

    The cleared cutoff is recorded in "cutoff_reached", so other computers sharing the
    settings stop their CLIs too: once, if they see it within CUTOFF_SYNC_WINDOW.
    What happened is noted in the provider's limits.
    """
    cfg = load_config()
    cutoffs, reached, handled = (_dict_setting(cfg, k) for k in ("cutoff", "cutoff_reached", "cutoff_handled"))
    now = time.time()
    todo = []  # (provider, cutoff, where it was reached)
    for u in results:
        cutoff = cutoff_for(cfg, u.name)
        top = top_limit(u)
        mark = reached.get(u.name)
        if cutoff is not None and top is not None and top.percent >= cutoff:
            cutoffs.pop(u.name, None)
            reached[u.name] = {"at": now, "cutoff": cutoff}
            handled[u.name] = now
            todo.append((u, cutoff, ""))
        elif (isinstance(mark, dict) and all(isinstance(mark.get(k), (int, float)) for k in ("at", "cutoff"))
              and abs(now - mark["at"]) < CUTOFF_SYNC_WINDOW and handled.get(u.name) != mark["at"]):
            handled[u.name] = mark["at"]
            todo.append((u, float(mark["cutoff"]), " on another computer"))
    if not todo:
        return
    running = find_cli_processes()
    for u, cutoff, where in todo:
        clis = cutoff_clis(u.name)
        stopped = sum(stop_process(pid) for pid, name in running if name in clis)
        what = " and ".join(sorted(clis)) or "CLI"
        if stopped:
            note = f"stopped {stopped} {what} process" + ("es" if stopped != 1 else "")
        else:
            note = f"no {what} running"
        u.limits.append(Limit(f"⛔ Cutoff {cutoff:.0f}% reached{where} — {note}; cutoff cleared", None))
    if cutoffs:
        cfg["cutoff"] = cutoffs
    else:
        cfg.pop("cutoff", None)
    cfg["cutoff_reached"], cfg["cutoff_handled"] = reached, handled
    save_config(cfg)


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
    "normal": "#4fa3ff",
    "warning": "#f0b429",
    "critical": "#ef5350",
    "error": "#ff8a80",
    "claude": "#d97757",
    "codex": "#10a37f",
    "bedrock": "#ff9900",
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
        self.fetching = False

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
        menu.add_command(label="Refresh now", command=self.refresh)
        menu.add_checkbutton(label="Always on top", variable=self.topmost_var, command=self._toggle_topmost)
        for name in cutoff_menu_providers(self.data):
            cutoff = cutoff_for(self.cfg, name)
            var = tk.IntVar(value=int(cutoff) if cutoff is not None else 0)
            self.cutoff_vars.append(var)  # Tk forgets variables Python no longer references
            sub = tk.Menu(menu, tearoff=0)
            for value in (0, *CUTOFF_CHOICES):
                sub.add_radiobutton(label=f"{value}%" if value else "Off", variable=var, value=value,
                                    command=lambda n=name, v=value: self._set_cutoff(n, v))
            menu.add_cascade(label=cutoff_menu_label(self.cfg, name), menu=sub)
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

    def _set_cutoff(self, name: str, value: int):
        self.cfg = set_cutoff(name, value or None)

    def _toggle_topmost(self):
        self.cfg = update_config(topmost=self.topmost_var.get())
        self.root.attributes("-topmost", self.cfg["topmost"])

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
            self.root.after(0, lambda: self._apply(results))

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, results: list[ProviderUsage]):
        self.fetching = False
        previous = {u.name: u for u in self.data}
        for u in results:
            # Keep the last good numbers on transient failures, flagged as stale.
            old = previous.get(u.name)
            if u.error and not u.limits and old and old.limits:
                u.limits, u.plan = old.limits, old.plan
        self.data = results
        self.cfg = self._load_config()  # a reached cutoff was cleared, or another computer changed a setting
        self.render()

    def _tick(self):
        # Poll on the configured interval; redraw countdowns every 30 s in between.
        now = time.time()
        last = max((u.fetched_at for u in self.data), default=0)
        if not self.fetching and now - last >= self.interval:
            self.refresh()
        elif self.data:
            self.render()
        self.root.after(30_000 if self.data else 1_000, self._tick)

    # -- drawing -------------------------------------------------------------

    def render(self):
        tk = self.tk
        for child in self.body.winfo_children():
            child.destroy()

        last = max((u.fetched_at for u in self.data), default=0)
        self.status.config(text="Updated " + time.strftime("%H:%M", time.localtime(last)) if last else "")

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
    "Codex": "https://chatgpt.com/codex/settings/usage",
    "Bedrock": "https://console.aws.amazon.com/costmanagement/home#/cost-explorer",
}


def usage_pages(data: list[ProviderUsage]) -> dict[str, str]:
    shown = {u.name for u in data}
    return {name: url for name, url in USAGE_PAGES.items() if name in shown or (not data and name != "Bedrock")}

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
        self.fetching = False
        self.last_fetch = 0.0

        # Icons are referenced by name from our own theme dir so that switching
        # between them actually repaints (AppIndicator caches by path).
        self.icon_dir = config_dir() / "icons"
        self.icon_dir.mkdir(parents=True, exist_ok=True)
        for level, (c1, c2, c3) in ICON_COLORS.items():
            (self.icon_dir / f"llm-usage-{level}.svg").write_text(ICON_SVG.format(c1=c1, c2=c2, c3=c3))

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
            self.GLib.idle_add(self._apply, results)

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, results: list[ProviderUsage]):
        self.fetching = False
        self.last_fetch = time.time()
        previous = {u.name: u for u in self.data}
        for u in results:
            old = previous.get(u.name)
            if u.error and not u.limits and old and old.limits:
                u.limits, u.plan = old.limits, old.plan
        self.data = results
        self.render()
        return False

    def _set_cutoff(self, _item, name: str, value: int):
        set_cutoff(name, value or None)
        self.GLib.idle_add(self.render)  # not from inside the menu's own signal handler

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
        self.ind.set_label("  ".join(parts) if parts else "…", "")
        self.ind.set_icon_full(f"llm-usage-{worst}", "LLM usage")

        menu = Gtk.Menu()

        def info(text):
            item = Gtk.MenuItem(label=text)
            item.set_sensitive(False)
            menu.append(item)

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
        cfg = load_config()
        for name in cutoff_menu_providers(self.data):
            cutoff = cutoff_for(cfg, name)
            sub = Gtk.Menu()
            for value in (0, *CUTOFF_CHOICES):
                choice = Gtk.CheckMenuItem(label=f"{value}%" if value else "Off")
                choice.set_draw_as_radio(True)
                choice.set_active(value == (cutoff or 0))
                choice.connect("activate", self._set_cutoff, name, value)
                sub.append(choice)
            item = Gtk.MenuItem(label=cutoff_menu_label(cfg, name))
            item.set_submenu(sub)
            menu.append(item)
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
            level = top.level()
            color = THEME.get(name.lower(), THEME["normal"]) if level == "normal" else THEME[level]
            draw.rectangle((x0, size - gap - height, x1, size - gap), fill=color)
    return img


class TaskbarStrip:
    """Windows: "Claude 99%  Codex 16%" on the taskbar, just left of the notification area.

    Windows has no API for putting text in the taskbar, so this is a small borderless
    always-on-top window kept over the taskbar's empty space: next to the tray when there
    is room, else in another gap (found with UI Automation), on two stacked lines if that
    is what fits. It follows the taskbar's theme and layout, steps aside for full-screen
    apps and an auto-hidden taskbar, and opens the tray menu when clicked. Runs on the
    main thread.
    """

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

    def _fullscreen_app(self, taskbar) -> bool:
        u = self.user32
        fg = u.GetForegroundWindow()
        if not fg or fg in (taskbar, self.hwnd):
            return False
        buf = self.ctypes.create_unicode_buffer(64)
        u.GetClassNameW(fg, buf, 64)
        if buf.value in ("Progman", "WorkerW"):
            return False  # the desktop
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
        font, bold = self.fonts[layout]
        pad = self.pad
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
        place = self._placement() if self.tray.cfg.get("taskbar_text", True) else None
        if place is None:
            self._hide()
            return
        taskbar, tb, tray_left = place
        top, bottom = tb[1], tb[3]
        now = time.monotonic()
        if self.spans is None or now - self.spans_at >= self.LAYOUT_MS / 1000:
            self.spans, self.spans_at = self._free_spans(taskbar, tb, tray_left), now
        margin = (bottom - top) // 8
        parts = self._parts()
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
                lparam = win32.WM_RBUTTONUP
            return on_notify(wparam, lparam)

        self.icon._message_handlers[win32.WM_NOTIFY] = handler
        self._win32 = win32

    def open_menu(self):
        """Show the tray menu at the mouse pointer (Windows; any thread)."""
        hwnd = getattr(self.icon, "_hwnd", None)
        if hwnd:
            self._win32.PostMessage(hwnd, self._win32.WM_NOTIFY, 0, self._win32.WM_LBUTTONUP)

    def _toggle_taskbar_text(self):
        self.cfg = update_config(taskbar_text=not self.cfg.get("taskbar_text", True))

    def _cutoff_action(self, name: str, value: int):
        def action():
            self.cfg = set_cutoff(name, value or None)
            if sys.platform != "win32":  # on Windows the menu is rebuilt when it is opened
                self._on_main(self._render)
        return action

    def _cutoff_checked(self, name: str, value: int):
        return lambda _item: (cutoff_for(self.cfg, name) or 0) == value

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
        for name in cutoff_menu_providers(self.data):
            items.append(item(cutoff_menu_label(self.cfg, name), self.pystray.Menu(*(
                item(f"{value}%" if value else "Off", self._cutoff_action(name, value),
                     checked=self._cutoff_checked(name, value), radio=True)
                for value in (0, *CUTOFF_CHOICES)))))
        if self.strip:
            items.append(item("Show usage on the taskbar", lambda: self._toggle_taskbar_text(),
                              checked=lambda _i: self.cfg.get("taskbar_text", True)))
        items.append(item("About", self.pystray.Menu(
            item(f"LLM Usage Widget — Version {__version__}", None, enabled=False),
            item(f"Source: {REPO_URL}", lambda: webbrowser.open(REPO_URL)),
            item(f"Aleph Void LLC: {LLC_URL}", lambda: webbrowser.open(LLC_URL)),
        )))
        items.append(item("Quit", lambda: self._quit()))
        return self.pystray.Menu(*items)

    def _render(self):
        text = summary_text(self.data)
        self.icon.icon = gauge_image(self.data)
        self.icon.title = text or "LLM Usage"  # tooltip
        if sys.platform != "win32":  # on Windows the menu is rebuilt when it is opened
            self.icon.menu = self._menu()
        if sys.platform == "darwin":
            # Show the summary next to the icon, like the Linux top bar.
            try:
                button = self.icon._status_item.button()
                button.setTitle_(" " + text if text else "")
                button.setImagePosition_(2)  # NSImageLeft
            except Exception:
                pass

    def _apply(self, results: list[ProviderUsage]):
        previous = {u.name: u for u in self.data}
        for u in results:
            old = previous.get(u.name)
            if u.error and not u.limits and old and old.limits:
                u.limits, u.plan = old.limits, old.plan
        self.data = results
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
                self._on_main(lambda r=results: self._apply(r))
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
