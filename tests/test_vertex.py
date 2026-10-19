"""Google Vertex AI: settings detection, Google credentials, Cloud Monitoring, the BigQuery billing export."""

from __future__ import annotations

import json
import os
import sys
import types
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

import usage_widget as w

UTC = timezone.utc
TOKEN_URL = "https://oauth2.googleapis.com/token"
MONITORING = "https://monitoring.googleapis.com/v3/projects/"
BIGQUERY = "https://bigquery.googleapis.com/bigquery/v2/projects/"
VERTEX_URL = "https://us-central1-aiplatform.googleapis.com/v1/projects/codex-proj/locations/us-central1/endpoints/openapi"


def write_claude_settings(home, env):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": env}), encoding="utf-8")


def write_codex_config(home, text):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def adc_path(home):
    """Where gcloud keeps the ADC file on this platform (APPDATA is under home, see conftest)."""
    base = Path(os.environ["APPDATA"]) if sys.platform == "win32" else home / ".config"
    return base / "gcloud" / "application_default_credentials.json"


def write_adc(home, data, path=None):
    path = path or adc_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


USER_ADC = {"type": "authorized_user", "client_id": "cid", "client_secret": "secret", "refresh_token": "rt",
            "quota_project_id": "quota-proj"}


# -- vertex_settings -------------------------------------------------------------

def test_settings_none_when_unused():
    assert w.vertex_settings() is None


def test_settings_disabled(home):
    write_claude_settings(home, {"CLAUDE_CODE_USE_VERTEX": "1"})
    w.save_config({"vertex": False})
    assert w.vertex_settings() is None


def test_settings_from_claude_code(home):
    write_claude_settings(home, {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "claude-proj"})
    assert w.vertex_settings() == {"projects": ["claude-proj"], "billing_table": "", "budget": 0.0,
                                   "cost_interval": w.COST_INTERVAL, "metrics_interval": w.METRICS_INTERVAL}


@pytest.mark.parametrize("var", ["GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"])
def test_settings_claude_project_fallbacks(home, var):
    write_claude_settings(home, {"CLAUDE_CODE_USE_VERTEX": "1", var: "p"})
    assert w.vertex_settings()["projects"] == ["p"]


def test_settings_from_codex(home):
    write_codex_config(home, f'base_url = "{VERTEX_URL}"\nbase_url = "https://aiplatform.googleapis.com/v1/x"\n')
    assert w.vertex_settings()["projects"] == ["codex-proj"]


def test_settings_combined_and_deduplicated(home):
    write_claude_settings(home, {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "codex-proj"})
    write_codex_config(home, f'base_url = "{VERTEX_URL}"\n')
    assert w.vertex_settings()["projects"] == ["codex-proj"]


def test_settings_config_overrides(home):
    write_claude_settings(home, {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "env-proj"})
    w.save_config({"vertex": {"projects": ["a", "b", "a"], "billing_table": "p.d.t", "budget": "250",
                              "cost_interval": 1, "metrics_interval": 600}})
    assert w.vertex_settings() == {"projects": ["a", "b"], "billing_table": "p.d.t", "budget": 250.0,
                                   "cost_interval": w.MIN_INTERVAL, "metrics_interval": 600}


def test_settings_single_project_key():
    w.save_config({"vertex": {"project": "solo"}})
    assert w.vertex_settings()["projects"] == ["solo"]


def test_settings_forced_on():
    w.save_config({"vertex": True})
    assert w.vertex_settings()["projects"] == []


# -- Application Default Credentials ---------------------------------------------

def test_adc_missing():
    assert w._gcp_adc() == {}


def test_adc_default_location(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    write_adc(home, USER_ADC)
    assert w._gcp_adc() == USER_ADC


def test_adc_windows_location(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(home / "Roaming"))
    write_adc(home, {"type": "x"}, home / "Roaming" / "gcloud" / "application_default_credentials.json")
    assert w._gcp_adc() == {"type": "x"}


def test_adc_cloudsdk_config(home, monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "sdk"))
    write_adc(home, {"type": "sdk"}, tmp_path / "sdk" / "application_default_credentials.json")
    assert w._gcp_adc() == {"type": "sdk"}


def test_adc_explicit_file(home, monkeypatch, tmp_path):
    path = write_adc(home, {"type": "service_account"}, tmp_path / "key.json")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    assert w._gcp_adc() == {"type": "service_account"}


@pytest.mark.parametrize("raw", ["{broken", "[]"])
def test_adc_garbage(home, raw):
    path = adc_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(raw)
    assert w._gcp_adc() == {}


# -- _gcp_token ------------------------------------------------------------------

def test_token_from_refresh_token(fake_http):
    fake_http.responses[TOKEN_URL] = {"access_token": "ya29.x", "expires_in": 3599}
    assert w._gcp_token(USER_ADC) == "ya29.x"
    assert fake_http.kwargs[0]["form"] == {"grant_type": "refresh_token", "client_id": "cid",
                                           "client_secret": "secret", "refresh_token": "rt"}
    assert fake_http.kwargs[0]["auth_errors"] is False
    # Cached for the rest of its life.
    assert w._gcp_token(USER_ADC) == "ya29.x"
    assert len(fake_http.calls) == 1


def test_token_refreshed_when_nearly_expired(fake_http, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(w.time, "time", lambda: clock[0])
    fake_http.responses[TOKEN_URL] = [{"access_token": "one", "expires_in": 100}, {"access_token": "two"}]
    assert w._gcp_token(USER_ADC) == "one"
    clock[0] += 41  # within a minute of expiry
    assert w._gcp_token(USER_ADC) == "two"


@pytest.mark.parametrize("code", [400, 401])
def test_token_refresh_rejected(fake_http, code):
    fake_http.responses[TOKEN_URL] = w.HTTPStatusError(code, "invalid_grant")
    with pytest.raises(w.AuthError, match="gcloud auth application-default login"):
        w._gcp_token(USER_ADC)


def test_token_refresh_server_error_propagates(fake_http):
    fake_http.responses[TOKEN_URL] = w.HTTPStatusError(503)
    with pytest.raises(w.HTTPStatusError):
        w._gcp_token(USER_ADC)


def test_token_from_google_auth(monkeypatch):
    refreshed = []

    class Creds:
        token = "from-google-auth"

        def refresh(self, request):
            refreshed.append(request)

    auth = types.ModuleType("google.auth")
    auth.default = lambda scopes: (Creds(), "proj")
    transport = types.ModuleType("google.auth.transport.requests")
    transport.Request = lambda: "request"
    google = types.ModuleType("google")
    google.auth = auth
    auth.transport = types.SimpleNamespace(requests=transport)
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.auth", auth)
    monkeypatch.setitem(sys.modules, "google.auth.transport", auth.transport)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", transport)
    assert w._gcp_token({"type": "service_account"}) == "from-google-auth"
    assert refreshed == ["request"]


def test_token_from_gcloud(monkeypatch):
    calls = []
    monkeypatch.setattr(w, "run_cli", lambda args, timeout=30: calls.append(args) or (
        "  cli-token\n" if args[2] == "print-access-token" else None))
    assert w._gcp_token({}) == "cli-token"
    assert calls == [["gcloud", "auth", "application-default", "print-access-token"],
                     ["gcloud", "auth", "print-access-token"]]


def test_no_google_credentials():
    with pytest.raises(w.AuthError, match="no Google Cloud credentials"):
        w._gcp_token({})


# -- Cloud Monitoring --------------------------------------------------------------

def test_vertex_tokens(fake_http, frozen_now):
    frozen_now(datetime(2025, 5, 6, 12, 0, tzinfo=UTC))
    page1 = {"timeSeries": [
        {"metric": {"labels": {"type": "input"}}, "points": [{"value": {"int64Value": "1000"}},
                                                             {"value": {"int64Value": "500"}}]},
        {"metric": {"labels": {"type": "output"}}, "points": [{"value": {"doubleValue": 25.5}}]},
        {"metric": {"labels": {"type": "cached"}}, "points": [{"value": {"int64Value": "9999"}}]},
        {"metric": {}},
    ], "nextPageToken": "next"}
    page2 = {"timeSeries": [{"metric": {"labels": {"type": "output"}}, "points": [{"value": {}}]}]}
    fake_http.responses[MONITORING] = [page1, page2]
    since = datetime(2025, 5, 6, 0, 0, tzinfo=UTC)
    assert w._vertex_tokens({"Authorization": "Bearer t"}, "my proj", since) == {"in": 1500.0, "out": 25.5}
    first, second = (urllib.parse.urlsplit(u) for u in fake_http.urls())
    assert first.path == "/v3/projects/my%20proj/timeSeries"
    q1 = urllib.parse.parse_qs(first.query)
    assert q1["filter"] == [f'metric.type="{w.VERTEX_TOKEN_METRIC}"']
    assert q1["interval.startTime"] == ["2025-05-06T00:00:00Z"]
    assert q1["interval.endTime"] == ["2025-05-06T12:00:00Z"]
    assert "pageToken" not in q1
    assert urllib.parse.parse_qs(second.query)["pageToken"] == ["next"]


# -- BigQuery billing export ---------------------------------------------------------

def test_vertex_month_cost(fake_http, frozen_now):
    frozen_now(datetime(2025, 7, 20, tzinfo=UTC))
    fake_http.responses[BIGQUERY] = {"jobComplete": True, "rows": [{"f": [{"v": "12.5"}, {"v": "EUR"}]}]}
    cost = w._vertex_month_cost({}, "billing-proj.export.gcp_billing_export_v1_ABC", ["p1", "p2"])
    assert cost == {"amount": 12.5, "unit": "EUR"}
    url = fake_http.urls()[0]
    assert url == BIGQUERY + "p1/queries"
    body = fake_http.kwargs[0]["body"]
    assert "`billing-proj.export.gcp_billing_export_v1_ABC`" in body["query"]
    params = {p["name"]: p["parameterValue"] for p in body["queryParameters"]}
    assert params["start"] == {"value": "2025-07-01"}
    assert params["month"] == {"value": "202507"}
    assert params["projects"] == {"arrayValues": [{"value": "p1"}, {"value": "p2"}]}
    assert body["useLegacySql"] is False


def test_vertex_month_cost_no_rows(fake_http):
    fake_http.responses[BIGQUERY] = {"jobComplete": True}
    assert w._vertex_month_cost({}, "a.b.c", ["p"]) == {"amount": 0.0, "unit": "USD"}


def test_vertex_month_cost_null_sum(fake_http):
    fake_http.responses[BIGQUERY] = {"jobComplete": True, "rows": [{"f": [{"v": None}, {"v": None}]}]}
    assert w._vertex_month_cost({}, "a.b.c", ["p"]) == {"amount": 0.0, "unit": "USD"}


def test_vertex_month_cost_incomplete(fake_http):
    fake_http.responses[BIGQUERY] = {"jobComplete": False}
    with pytest.raises(RuntimeError, match="still running"):
        w._vertex_month_cost({}, "a.b.c", ["p"])


@pytest.mark.parametrize("table", ["just_a_table", "a.b", "a.b.c`; DROP TABLE x; --", "a.b.c d", ""])
def test_vertex_month_cost_rejects_bad_table_names(fake_http, table):
    with pytest.raises(ValueError, match="project.dataset.table"):
        w._vertex_month_cost({}, table, ["p"])
    assert fake_http.calls == []


# -- fetch_vertex ------------------------------------------------------------------

@pytest.fixture
def gcp_login(home, fake_http):
    write_adc(home, USER_ADC)
    fake_http.responses[TOKEN_URL] = {"access_token": "ya29.tok", "expires_in": 3600}
    return fake_http


def monitoring_response(tokens_in, tokens_out):
    return {"timeSeries": [
        {"metric": {"labels": {"type": "input"}}, "points": [{"value": {"int64Value": str(tokens_in)}}]},
        {"metric": {"labels": {"type": "output"}}, "points": [{"value": {"int64Value": str(tokens_out)}}]},
    ]}


def test_fetch_vertex_not_used():
    with pytest.raises(w.NotUsed):
        w.fetch_vertex()


def test_fetch_vertex_tokens_only(gcp_login):
    w.save_config({"vertex": {"projects": ["p1", "p2"]}})
    gcp_login.responses[MONITORING] = lambda url, kw: monitoring_response(1000, 500)
    usage = w.fetch_vertex()
    assert usage.name == "Vertex"
    assert [lim.label for lim in usage.limits] == ["Tokens today 2.0k in · 1.0k out"]
    assert usage.summary == "3.0k tok"
    assert sorted(urllib.parse.urlsplit(u).path for u in gcp_login.urls(MONITORING)) == [
        "/v3/projects/p1/timeSeries", "/v3/projects/p2/timeSeries"]
    headers = gcp_login.calls[-1][1]
    assert headers == {"Authorization": "Bearer ya29.tok", "x-goog-user-project": "quota-proj"}


def test_fetch_vertex_with_billing_and_budget(gcp_login):
    w.save_config({"vertex": {"projects": ["p1"], "billing_table": "b.d.t", "budget": 100}})
    gcp_login.responses[BIGQUERY] = {"jobComplete": True, "rows": [{"f": [{"v": "80"}, {"v": "USD"}]}]}
    gcp_login.responses[MONITORING] = monitoring_response(10, 5)
    usage = w.fetch_vertex()
    budget, tokens = usage.limits
    assert budget.label == "This month $80.00 / $100.00"
    assert budget.percent == 80.0
    assert budget.resets_at is not None
    assert tokens.label == "Tokens today 10 in · 5 out"
    assert usage.summary == "$80.00"


def test_fetch_vertex_with_billing_no_budget(gcp_login):
    w.save_config({"vertex": {"projects": ["p1"], "billing_table": "b.d.t"}})
    gcp_login.responses[BIGQUERY] = {"jobComplete": True, "rows": [{"f": [{"v": "3"}, {"v": "USD"}]}]}
    gcp_login.responses[MONITORING] = monitoring_response(0, 0)
    month, _ = w.fetch_vertex().limits
    assert (month.label, month.percent) == ("This month $3.00", None)


def test_fetch_vertex_caches(gcp_login):
    w.save_config({"vertex": {"projects": ["p1"]}})
    gcp_login.responses[MONITORING] = monitoring_response(1, 1)
    w.fetch_vertex()
    w.fetch_vertex()
    assert len(gcp_login.urls(MONITORING)) == 1


def test_fetch_vertex_project_from_quota_project(gcp_login):
    w.save_config({"vertex": True})
    gcp_login.responses[MONITORING] = monitoring_response(1, 1)
    w.fetch_vertex()
    assert "/projects/quota-proj/" in gcp_login.urls(MONITORING)[0]


def test_fetch_vertex_project_from_gcloud(home, fake_http, monkeypatch):
    write_adc(home, {**USER_ADC, "quota_project_id": ""})
    fake_http.responses[TOKEN_URL] = {"access_token": "t"}
    fake_http.responses[MONITORING] = monitoring_response(1, 1)
    monkeypatch.setattr(w, "run_cli", lambda args, timeout=30: "gcloud-proj\n" if args[1] == "config" else None)
    w.save_config({"vertex": True})
    w.fetch_vertex()
    assert "/projects/gcloud-proj/" in fake_http.urls(MONITORING)[0]
    assert "x-goog-user-project" not in fake_http.calls[-1][1]


def test_fetch_vertex_no_project(home, fake_http):
    write_adc(home, {**USER_ADC, "quota_project_id": ""})
    fake_http.responses[TOKEN_URL] = {"access_token": "t"}
    w.save_config({"vertex": True})
    with pytest.raises(RuntimeError, match="no Google Cloud project"):
        w.fetch_vertex()


def test_fetch_vertex_no_credentials():
    w.save_config({"vertex": {"projects": ["p"]}})
    with pytest.raises(w.AuthError, match="no Google Cloud credentials"):
        w.fetch_vertex()


def test_fetch_vertex_expired_login(gcp_login):
    w.save_config({"vertex": {"projects": ["p"]}})
    gcp_login.responses[MONITORING] = w.HTTPStatusError(401, "Request had invalid authentication credentials")
    with pytest.raises(w.AuthError, match="Google login expired"):
        w.fetch_vertex()


def test_fetch_vertex_errors_become_notes(gcp_login):
    w.save_config({"vertex": {"projects": ["p1"], "billing_table": "not-a-table"}})
    gcp_login.responses[MONITORING] = w.HTTPStatusError(403, "Permission monitoring.timeSeries.list denied")
    usage = w.fetch_vertex()
    assert [lim.label for lim in usage.limits] == [
        "⚠ billing export: billing_table should look like project.dataset.table",
        "⚠ Monitoring p1: HTTP 403: Permission monitoring.timeSeries.list denied",
    ]
    assert usage.summary == ""
    assert w.summary_text([usage]) == ""


def test_fetch_vertex_network_error_becomes_note(gcp_login):
    w.save_config({"vertex": {"projects": ["p1"]}})
    gcp_login.responses[MONITORING] = RuntimeError("network: timed out")
    assert w.fetch_vertex().limits[0].label == "⚠ Monitoring p1: network: timed out"
