"""Qwen Cloud: the Firefox login, the console gateway calls and the Token Plan usage reply."""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone

import pytest

import qwen_cloud as q
import usage_widget as w

INFO_URL = "https://home.qwencloud.com/tool/user/info.json"
GATEWAY = q.GATEWAY
RESET_MS = 1794326400000  # 2026-11-10 16:00 UTC

SNAP_PROFILES = ("snap", "firefox", "common", ".mozilla", "firefox")


def write_firefox(home, cookies, profile="abc.default", root=(".mozilla", "firefox"), wal=False):
    """A Firefox cookie store holding (host, name, value, expiry) rows."""
    db = home.joinpath(*root, profile, "cookies.sqlite")
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE IF NOT EXISTS moz_cookies (id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT,"
                " expiry INTEGER)")
    con.executemany("INSERT INTO moz_cookies (host, name, value, expiry) VALUES (?, ?, ?, ?)", cookies)
    con.commit()
    if not wal:
        con.close()
        return db, None
    return db, con  # left open, so the rows stay in the write-ahead log


LOGGED_IN = [(".qwencloud.com", "login_qwencloud_ticket", "TICKET", 0),
             (".qwencloud.com", "isg", "ISG", 4102444800),
             ("home.qwencloud.com", "maas-region", "ap", 0)]


def gateway_api(kwargs) -> str:
    return json.loads(kwargs["form"]["params"])["Api"].rsplit("/", 1)[-1]


@pytest.fixture
def qwen(home, fake_http):
    """Logged in to Firefox; the console answers like a Lite plan with only a monthly window left."""
    write_firefox(home, LOGGED_IN)
    fake_http.responses[INFO_URL] = {"code": "200", "data": {"secToken": "SEC"}}
    replies = {
        "usage": {"code": "200", "successResponse": True, "data": {"DataV2": {"data": json.dumps(
            {"success": True, "data": {"per1MonthPercentage": 0.055, "per1MonthResetTime": RESET_MS}})}}},
        "subscription": {"successResponse": True, "data": {"specCode": "lite", "status": "VALID"}},
        "quota-config": {"successResponse": True, "data": json.dumps(
            {"lite": {"monthly": 20000}, "pro": {"five_hour": 12000, "monthly": 200000}})},
    }
    fake_http.responses[GATEWAY] = lambda url, kwargs: replies[gateway_api(kwargs)]
    fake_http.replies = replies
    return fake_http


# -- the provider ----------------------------------------------------------------

def test_monthly_plan(qwen):
    u = w.fetch_qwen()
    assert u.name == "Qwen" and u.plan == "Lite" and u.error == ""
    [lim] = u.limits
    assert lim.label == "Monthly · 1.1k / 20.0k credits"
    assert lim.percent == pytest.approx(5.5)
    assert lim.resets_at == datetime(2026, 11, 10, 16, tzinfo=timezone.utc)


def test_requests(qwen):
    w.fetch_qwen()
    info_url, info_headers = qwen.calls[0]
    assert info_url == INFO_URL
    # Each host gets only the cookies a browser would send it: the host-only one stays on home.qwencloud.com.
    assert info_headers["Cookie"] == "login_qwencloud_ticket=TICKET; isg=ISG; maas-region=ap"
    gateway = [(url, headers, kwargs) for (url, headers), kwargs in zip(qwen.calls, qwen.kwargs) if url.startswith(GATEWAY)]
    assert [gateway_api(k) for _, _, k in gateway] == ["usage", "subscription", "quota-config"]
    url, headers, kwargs = gateway[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["api"] == ["zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/usage"]
    assert headers["Cookie"] == "login_qwencloud_ticket=TICKET; isg=ISG"
    assert headers["Origin"] == "https://home.qwencloud.com" and headers["Referer"] == q.DASHBOARD
    assert kwargs["auth_errors"] is False
    form = kwargs["form"]
    assert form["sec_token"] == "SEC" and form["product"] == "sfm_bailian" and form["region"] == "ap-southeast-1"
    params = json.loads(form["params"])
    assert params["V"] == "1.0"
    assert params["Data"]["cornerstoneParam"]["consoleSite"] == "QWENCLOUD"
    assert json.loads(gateway[1][2]["form"]["params"])["Data"]["commodityCode"] == q.COMMODITY


def test_all_three_windows(qwen):
    qwen.replies["usage"] = {"successResponse": True, "data": {
        "per5HourPercentage": "0.25", "per5HourResetTime": RESET_MS // 1000,
        "per1WeekPercentage": 1.2, "per1WeekResetTime": "2026-11-16T00:00:00Z",
        "per1MonthPercentage": 0, "per1MonthResetTime": RESET_MS}}
    qwen.replies["subscription"] = {"data": {"specCode": "pro"}}
    u = w.fetch_qwen()
    assert u.plan == "Pro"
    assert [(lim.label, lim.percent) for lim in u.limits] == [
        ("Session (5h) · 3.0k / 12.0k credits", 25), ("Weekly", 100), ("Monthly · 0 / 200.0k credits", 0)]
    assert u.limits[0].resets_at == datetime(2026, 11, 10, 16, tzinfo=timezone.utc)
    assert u.limits[1].resets_at == datetime(2026, 11, 16, tzinfo=timezone.utc)


def test_plan_details_are_optional(qwen):
    qwen.replies["subscription"] = RuntimeError("HTTP 500")
    u = w.fetch_qwen()
    assert u.plan == "" and u.limits[0].label == "Monthly"


def test_plan_details_are_cached(qwen):
    w.fetch_qwen()
    w.fetch_qwen()
    apis = [gateway_api(k) for (url, _), k in zip(qwen.calls, qwen.kwargs) if url.startswith(GATEWAY)]
    assert apis == ["usage", "subscription", "quota-config", "usage"]


def test_no_token_plan(qwen):
    qwen.replies["usage"] = {"successResponse": True, "data": {"success": True, "data": {}}}
    with pytest.raises(RuntimeError, match="Token Plan active"):
        w.fetch_qwen()


def test_unusable_ratios(qwen):
    qwen.replies["usage"] = {"data": {"per1MonthPercentage": True, "per1WeekPercentage": "nope"}}
    with pytest.raises(RuntimeError, match="Token Plan active"):
        w.fetch_qwen()


@pytest.mark.parametrize("reply", [
    {"successResponse": False, "code": "ConsoleNeedLogin", "message": "need login"},
    {"successResponse": False, "code": "PostonlyOrTokenError", "message": "token invalid"},
    {"successResponse": False, "code": "403"},
])
def test_gateway_login_errors(qwen, reply):
    qwen.replies["usage"] = reply
    with pytest.raises(w.AuthError, match="home.qwencloud.com"):
        w.fetch_qwen()


def test_gateway_other_error(qwen):
    qwen.replies["usage"] = {"successResponse": False, "code": "Throttling", "message": "Request was\n denied"}
    with pytest.raises(RuntimeError, match="^Request was denied$"):
        w.fetch_qwen()


@pytest.mark.parametrize("error", [w.HTTPStatusError(302), w.HTTPStatusError(401),
                                   json.JSONDecodeError("Expecting value", "<html>", 0)])
def test_expired_login(qwen, error):
    qwen.responses[INFO_URL] = error
    with pytest.raises(w.AuthError, match="login expired"):
        w.fetch_qwen()


def test_server_error_is_not_a_login_problem(qwen):
    qwen.responses[INFO_URL] = w.HTTPStatusError(503)
    with pytest.raises(w.HTTPStatusError):
        w.fetch_qwen()


def test_user_info_without_token(qwen):
    qwen.responses[INFO_URL] = {"data": {"secToken": ""}}
    with pytest.raises(w.AuthError, match="login expired"):
        w.fetch_qwen()


def test_sec_token_cookie_skips_user_info(home, qwen):
    write_firefox(home, [(".qwencloud.com", "sec_token", "FROMCOOKIE", 0)])
    w.fetch_qwen()
    assert not qwen.urls(INFO_URL)
    assert qwen.kwargs[0]["form"]["sec_token"] == "FROMCOOKIE"


def test_csrf_and_anonymous_id(home, qwen):
    write_firefox(home, [(".qwencloud.com", "login_aliyunid_csrf", "CSRF", 0), (".qwencloud.com", "cna", "ANON", 0)])
    w.fetch_qwen()
    headers = next(h for url, h in qwen.calls if url.startswith(GATEWAY))
    assert headers["x-csrf-token"] == headers["x-xsrf-token"] == "CSRF"
    params = json.loads(next(k for (url, _), k in zip(qwen.calls, qwen.kwargs) if url.startswith(GATEWAY))["form"]["params"])
    assert params["Data"]["cornerstoneParam"]["X-Anonymous-Id"] == "ANON"


# -- where the login comes from ------------------------------------------------------

def test_not_used_without_a_login():
    with pytest.raises(w.NotUsed):
        w.fetch_qwen()


def test_turned_on_without_a_login(home):
    w.save_config({"qwen": True})
    with pytest.raises(w.AuthError, match="not logged in"):
        w.fetch_qwen()


def test_turned_off(qwen):
    w.save_config({"qwen": False})
    with pytest.raises(w.NotUsed):
        w.fetch_qwen()
    assert not qwen.calls


def test_cookie_from_config(home, fake_http):
    w.save_config({"qwen": {"cookie": "Cookie: login_qwencloud_ticket=CFG;\n sec_token=S"}})
    assert w.qwen_cookies() == ("login_qwencloud_ticket=CFG; sec_token=S",) * 2


def test_cookie_from_environment(monkeypatch):
    monkeypatch.setenv("QWEN_CLOUD_COOKIE", " a=1 ")
    assert w.qwen_cookies() == ("a=1", "a=1")


def test_snap_firefox_and_expired_cookies(home):
    write_firefox(home, [(".qwencloud.com", "login_qwencloud_ticket", "T", int(time.time() * 1000) + 60000),
                         (".qwencloud.com", "old", "x", int(time.time()) - 60),
                         (".qwencloud.com.evil.com", "nope", "x", 0),
                         (".example.com", "other", "x", 0)], root=SNAP_PROFILES)
    assert w.qwen_cookies() == ("login_qwencloud_ticket=T",) * 2


def test_prefers_the_logged_in_profile(home):
    write_firefox(home, [(".qwencloud.com", "isg", "ANON", 0)], profile="b.other")
    write_firefox(home, [(".qwencloud.com", "login_qwencloud_ticket", "T", 0)], profile="a.default")
    assert w.qwen_cookies()[0] == "login_qwencloud_ticket=T"


def test_reads_the_write_ahead_log(home):
    db, con = write_firefox(home, [(".qwencloud.com", "login_qwencloud_ticket", "FRESH", 0)], wal=True)
    try:
        assert db.with_name("cookies.sqlite-wal").exists()
        assert w.qwen_cookies()[0] == "login_qwencloud_ticket=FRESH"
    finally:
        con.close()


def test_damaged_store_is_skipped(home):
    db = home / ".mozilla" / "firefox" / "x.default" / "cookies.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a database")
    with pytest.raises(w.NotUsed):
        w.fetch_qwen()


def test_unchanged_store_is_not_copied_again(home, monkeypatch):
    write_firefox(home, LOGGED_IN)
    w.qwen_cookies()
    monkeypatch.setattr(q.shutil, "copyfile", lambda *a: pytest.fail("copied again"))
    assert w.qwen_cookies()[0].startswith("login_qwencloud_ticket=TICKET")


def test_cookie_header_rules():
    cookies = [(".qwencloud.com", "a", "domain"), ("home.qwencloud.com", "a", "host"),
               ("qwencloud.com", "b", "bare-host-only"), (".home.qwencloud.com", "c", "sub")]
    assert q.cookie_header(cookies, "home.qwencloud.com") == "a=host; c=sub"
    assert q.cookie_header(cookies, "cs-data.qwencloud.com") == "a=domain"
    assert q.cookie_header(cookies, "qwencloud.com") == "a=domain; b=bare-host-only"


# -- cutoffs and the menu -------------------------------------------------------------

def test_cutoff_stops_clis_on_qwen(home, monkeypatch):
    assert w.cutoff_clis("Qwen") == set()
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"env": {"ANTHROPIC_BASE_URL": "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"}}))
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('base_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"\n')
    assert w.cutoff_clis("Qwen") == {"claude", "codex"}


def test_qwen_backed_clis_hide_their_own_sections(home):
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"env": {"ANTHROPIC_BASE_URL": "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"}}))
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('base_url = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/v1"\n')
    with pytest.raises(w.NotUsed):
        w._claude_credentials()
    with pytest.raises(w.NotUsed):
        w._codex_tokens()


def test_usage_page_only_when_in_use():
    assert "Qwen" not in w.usage_pages([])
    assert w.usage_pages([w.ProviderUsage("Qwen")]) == {"Qwen": q.DASHBOARD}


def test_in_fetch_all(qwen):
    names = [u.name for u in w.fetch_all()]
    assert names[-1] == "Qwen"
    assert w.summary_text([u for u in w.fetch_all() if u.name == "Qwen"]) == "Qwen 6%"


# -- qwen_cloud on its own ------------------------------------------------------------

def test_module_stands_alone():
    # The widget passes in its HTTP function; the module never reaches back into usage_widget.
    source = (w.Path(q.__file__)).read_text(encoding="utf-8")
    assert "usage_widget" not in source.split('"""', 2)[2]


def test_fetch_usage_with_a_plain_request_function():
    calls = []

    def request(url, headers, form):
        calls.append(url)
        if url.endswith("/tool/user/info.json"):
            return {"data": {"secToken": "T"}}
        api = json.loads(form["params"])["Api"].rsplit("/", 1)[-1]
        return {"usage": {"data": {"per1WeekPercentage": 0.5}},
                "subscription": {"data": {"specCode": "standard"}},
                "quota-config": {"standard": {"weekly": 1000}}}[api]

    usage = q.fetch_usage("login_qwencloud_ticket=x", "login_qwencloud_ticket=x", request)
    assert usage.plan == "Standard"
    assert usage.windows == [q.Window("Weekly", 50.0, None, 1000.0)]
    assert usage.windows[0].used == 500
    assert len(calls) == 4


def test_login_required_from_the_request_function():
    def request(url, headers, form):
        raise q.LoginRequired("HTTP 302")

    with pytest.raises(q.LoginRequired):
        q.fetch_usage("a=1", "a=1", request)


@pytest.mark.parametrize("value, expected", [
    (1794326400000, datetime(2026, 11, 10, 16, tzinfo=timezone.utc)),
    (1794326400, datetime(2026, 11, 10, 16, tzinfo=timezone.utc)),
    ("2026-11-10T16:00:00Z", datetime(2026, 11, 10, 16, tzinfo=timezone.utc)),
    ("2026-11-10 16:00:00", datetime(2026, 11, 10, 16, tzinfo=timezone.utc)),
    (0, None), (-5, None), (True, None), ("soon", None), (None, None), (1e300, None),
])
def test_timestamp(value, expected):
    assert q.timestamp(value) == expected


def test_expand_json_leaves_broken_json_alone():
    assert q.expand_json({"a": "[1, 2", "b": '{"c": "[3]"}'}) == {"a": "[1, 2", "b": {"c": [3]}}


def test_clean_cookie_header():
    assert q.clean_cookie_header("cookie:\ta=1;\r\n b=2 ") == "a=1; b=2"
