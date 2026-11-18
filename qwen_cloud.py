"""Qwen Cloud Token Plan usage, read through the home.qwencloud.com console.

Qwen Cloud has no public quota API: the Token Plan key (sk-sp-…) can only call
models, and the qwencloud CLI does not see Individual plans. So this asks the
same console gateway the Token Plan page uses (as CodexBar's Qwen Cloud
provider does), with the user's home.qwencloud.com login. That login can be
read from Firefox's cookie store: read-only, from a copy, and only the
qwencloud.com cookies.

Standard library only, and independent of usage_widget: the caller passes in
the HTTP function, so the widget keeps one place that talks to the network.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

HOME = "https://home.qwencloud.com"
DASHBOARD = f"{HOME}/billing/subscription/token-plan-individual"
GATEWAY = "https://cs-data.qwencloud.com/data/api.json"
API = "zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/"
PRODUCT = "sfm_bailian"
ACTION = "IntlBroadScopeAspnGateway"
COMMODITY = "sfm_tokenplansolo_public_intl"
COOKIE_DOMAIN = "qwencloud.com"
# (ratio key, reset key, quota-config key, label) of each window the usage reply can carry.
WINDOWS = (("per5HourPercentage", "per5HourResetTime", "five_hour", "Session (5h)"),
           ("per1WeekPercentage", "per1WeekResetTime", "weekly", "Weekly"),
           ("per1MonthPercentage", "per1MonthResetTime", "monthly", "Monthly"))
# Claude Code or Codex pointed at a Qwen / Alibaba Model Studio endpoint draw on this quota.
HOST_RE = re.compile(r"maas\.aliyuncs\.com|dashscope(?:-intl)?\.aliyuncs\.com|qwencloud\.com", re.IGNORECASE)
NO_PLAN = "no Token Plan usage in the reply — is a Token Plan active on this account?"

# request(url, headers, form) -> the parsed JSON reply. It raises LoginRequired when the server
# rejects the login (401/403, a redirect to the login page, an HTML page where JSON was expected).
Request = Callable[[str, dict, Optional[dict]], dict]


class LoginRequired(Exception):
    """The console login is missing, expired or rejected."""


@dataclass
class Window:
    label: str  # "Session (5h)", "Weekly", "Monthly"
    percent: float  # 0-100 used
    resets_at: datetime | None = None
    total: float | None = None  # the window's credits, when the plan's quota config gives them

    @property
    def used(self) -> float | None:
        return self.total * self.percent / 100 if self.total is not None else None


@dataclass
class Usage:
    plan: str = ""  # "Lite", "Essential", "Standard", "Pro"; "" when unknown
    windows: list[Window] = field(default_factory=list)


# --------------------------------------------------------------------------
# The login: Firefox's cookie store, or a pasted Cookie header
# --------------------------------------------------------------------------

_FIREFOX_CACHE: dict = {}  # cookie-store snapshot key -> cookies, so an unchanged store isn't copied again


def firefox_cookie_dbs() -> list[Path]:
    """Every Firefox profile's cookie store (regular, Snap, Flatpak, macOS, Windows), most recently used first."""
    home = Path.home()
    roots = [home / ".mozilla" / "firefox", home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
             home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
             home / ".config" / "mozilla" / "firefox",
             home / "Library" / "Application Support" / "Firefox" / "Profiles"]
    if os.environ.get("APPDATA"):
        roots.append(Path(os.environ["APPDATA"]) / "Mozilla" / "Firefox" / "Profiles")
    found = []
    for root in roots:
        try:
            for db in root.glob("*/cookies.sqlite"):
                found.append((db.stat().st_mtime, db))
        except OSError:
            continue
    return [db for _, db in sorted(found, key=lambda item: item[0], reverse=True)]


def read_firefox_cookies(db: Path, domain: str) -> list[tuple[str, str, str]]:
    """(host, name, value) of the unexpired cookies in one cookie store for `domain` and its subdomains.

    Firefox keeps the store open, so this reads a copy (with its write-ahead log, where recent
    logins may still be), never the original.
    """
    import sqlite3

    wal = db.with_name(db.name + "-wal")
    key = (str(db), domain, *(p.stat().st_mtime_ns if p.exists() else 0 for p in (db, wal)))
    if key in _FIREFOX_CACHE:
        return _FIREFOX_CACHE[key]
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "cookies.sqlite"
        shutil.copyfile(db, copy)
        if wal.exists():
            shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        con = sqlite3.connect(str(copy))
        try:
            rows = con.execute("SELECT host, name, value, expiry FROM moz_cookies WHERE host IN (?, ?) OR host LIKE ?",
                               (domain, "." + domain, "%." + domain)).fetchall()
        finally:
            con.close()
    now = time.time()
    cookies = []
    for host, name, value, expiry in rows:
        # Older Firefox stores expiry in seconds, newer in milliseconds; 0 is a session cookie.
        expires = expiry / 1000 if isinstance(expiry, (int, float)) and expiry > 1e11 else expiry
        if name and isinstance(value, str) and (not expires or expires > now):
            cookies.append((str(host), str(name), value))
    _FIREFOX_CACHE.clear()
    _FIREFOX_CACHE[key] = cookies
    return cookies


def firefox_cookies(domain: str = COOKIE_DOMAIN) -> list[tuple[str, str, str]]:
    """Firefox's cookies for `domain`, from the most recently used profile that is logged in there
    (has a *_ticket cookie), else from the first profile that has any."""
    first: list[tuple[str, str, str]] = []
    for db in firefox_cookie_dbs():
        try:
            cookies = read_firefox_cookies(db, domain)
        except Exception:  # a locked, damaged or foreign-format store: try the next profile
            continue
        if any(name.endswith("_ticket") for _, name, _ in cookies):
            return cookies
        first = first or cookies
    return first


def cookie_header(cookies: list[tuple[str, str, str]], host: str) -> str:
    """The Cookie header a browser would send to `host`: domain cookies (".example.com") for it and
    its subdomains, host-only cookies only for their own host; the most specific one wins a name."""
    chosen: dict[str, tuple[int, str]] = {}
    for cookie_host, name, value in cookies:
        bare = cookie_host.lstrip(".")
        if host == bare or (cookie_host.startswith(".") and host.endswith("." + bare)):
            if name not in chosen or len(bare) > chosen[name][0]:
                chosen[name] = (len(bare), value)
    return "; ".join(f"{name}={value}" for name, (_, value) in chosen.items())


def console_cookies(cookies: list[tuple[str, str, str]]) -> tuple[str, str]:
    """(Cookie header for the console, for the data gateway) from a browser's cookies."""
    return (cookie_header(cookies, urllib.parse.urlsplit(HOME).hostname),
            cookie_header(cookies, urllib.parse.urlsplit(GATEWAY).hostname))


def clean_cookie_header(header: str) -> str:
    """A pasted header without a "Cookie:" prefix or line breaks (which can't go in a header)."""
    header = " ".join(header.split())
    if header.lower().startswith("cookie:"):
        header = header[7:].strip()
    return header


def cookie_value(header: str, name: str) -> str:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == name.lower() and value:
            return value
    return ""


# --------------------------------------------------------------------------
# Reading the replies
# --------------------------------------------------------------------------

def expand_json(value):
    """Decode JSON objects and arrays nested as strings, as the console gateway returns them."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "["):
            try:
                return expand_json(json.loads(text))
            except ValueError:
                return value
        return value
    if isinstance(value, dict):
        return {k: expand_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_json(v) for v in value]
    return value


def find_object(value, keys) -> dict | None:
    """The first dict anywhere in `value` that has any of `keys`."""
    if isinstance(value, dict):
        if any(k in value for k in keys):
            return value
        children = list(value.values())
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = find_object(child, keys)
        if found is not None:
            return found
    return None


def number(value) -> float | None:
    """A finite number from a JSON number or numeric string; None for booleans and anything else."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def timestamp(value) -> datetime | None:
    """An epoch in seconds or milliseconds, or an ISO 8601 string, as an aware UTC datetime."""
    seconds = number(value)
    try:
        if seconds is not None:
            if seconds <= 0:
                return None
            return datetime.fromtimestamp(seconds / 1000 if seconds >= 1e12 else seconds, tz=timezone.utc)
        if isinstance(value, str) and value:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        pass
    return None


# --------------------------------------------------------------------------
# The console gateway
# --------------------------------------------------------------------------

def sec_token(cookie: str, request: Request) -> str:
    """The console's CSRF token, which every gateway call needs: a sec_token cookie, else the user-info endpoint."""
    token = cookie_value(cookie, "sec_token")
    if token:
        return token
    info = expand_json(request(f"{HOME}/tool/user/info.json", {"Cookie": cookie, "Referer": DASHBOARD}, None))
    found = find_object(info, ("secToken", "sec_token")) or {}
    token = found.get("secToken") or found.get("sec_token")
    if not isinstance(token, str) or not token:
        raise LoginRequired("no sec_token in the user info")
    return token


def call_api(api: str, data: dict, cookie: str, token: str, request: Request):
    """One Token Plan API call through the console gateway, its reply with nested JSON decoded."""
    cornerstone = {"feTraceId": str(uuid.uuid4()), "feURL": DASHBOARD, "protocol": "V2",
                   "console": "ONE_CONSOLE", "productCode": "p_efm", "domain": urllib.parse.urlsplit(HOME).hostname,
                   "consoleSite": "QWENCLOUD", "userNickName": "", "userPrincipalName": "", "xsp_lang": "en-US"}
    anonymous_id = cookie_value(cookie, "cna")
    if anonymous_id:
        cornerstone["X-Anonymous-Id"] = anonymous_id
    params = {"Api": API + api, "V": "1.0", "Data": {**data, "cornerstoneParam": cornerstone}}
    headers = {"Cookie": cookie, "Origin": HOME, "Referer": DASHBOARD,
               "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}
    csrf = cookie_value(cookie, "login_aliyunid_csrf") or cookie_value(cookie, "csrf")
    if csrf:
        headers["x-xsrf-token"] = headers["x-csrf-token"] = csrf
    query = urllib.parse.urlencode({"action": ACTION, "product": PRODUCT, "api": API + api, "_v": "undefined"})
    reply = expand_json(request(f"{GATEWAY}?{query}", headers, {
        "product": PRODUCT, "action": ACTION, "sec_token": token, "region": "ap-southeast-1",
        "language": "en-US", "params": json.dumps(params, separators=(",", ":"))}))
    if isinstance(reply, dict) and reply.get("successResponse") is False:
        code = str(reply.get("code") or reply.get("errorCode") or "")
        message = str(reply.get("message") or reply.get("errorMsg") or code or "request failed")
        if code in ("401", "403") or re.search(r"login|token|session", code + " " + message, re.IGNORECASE):
            raise LoginRequired(message)
        raise RuntimeError(" ".join(message.split())[:90])
    return reply


def plan_info(cookie: str, token: str, request: Request) -> dict:
    """{"plan": "lite", "quota": {"monthly": 20000, ...}} from the subscription and quota-config calls.

    Either may be missing: they only add the plan name and credit totals, so their failures are ignored.
    """
    plan, quota = "", {}
    try:
        found = find_object(call_api("subscription", {"commodityCode": COMMODITY}, cookie, token, request),
                            ("specCode", "spec_code", "planName", "plan_name"))
        for key in ("specCode", "spec_code", "planName", "plan_name"):
            if found and isinstance(found.get(key), str) and found[key].strip():
                plan = found[key].strip().lower()
                break
        if plan:
            totals = find_object(call_api("quota-config", {}, cookie, token, request), (plan,))
            if totals and isinstance(totals.get(plan), dict):
                quota = {k: v for k, v in totals[plan].items() if number(v) is not None}
    except (LoginRequired, RuntimeError, OSError, ValueError):
        pass
    return {"plan": plan, "quota": quota}


def fetch_usage(home_cookie: str, api_cookie: str, request: Request,
                cached: Callable[[str, Callable[[], dict]], dict] | None = None) -> Usage:
    """The Token Plan's usage windows. `cached(key, compute)` may remember the plan details between calls."""
    token = sec_token(home_cookie, request)
    windows = find_object(call_api("usage", {}, api_cookie, token, request), [w[0] for w in WINDOWS])
    if windows is None:
        raise RuntimeError(NO_PLAN)
    compute = lambda: plan_info(api_cookie, token, request)  # noqa: E731
    info = cached("qwen:plan", compute) if cached else compute()
    plan = str(info.get("plan") or "")
    quota = info.get("quota") if isinstance(info.get("quota"), dict) else {}
    usage = Usage(plan.capitalize() if plan.isalpha() else plan)
    for ratio_key, reset_key, quota_key, label in WINDOWS:
        ratio = number(windows.get(ratio_key))
        if ratio is None:
            continue
        total = number(quota.get(quota_key) or (quota.get("fiveHour") if quota_key == "five_hour" else None))
        usage.windows.append(Window(label, min(max(ratio, 0.0), 1.0) * 100, timestamp(windows.get(reset_key)),
                                    total if total and total > 0 else None))
    if not usage.windows:
        raise RuntimeError(NO_PLAN)
    return usage
