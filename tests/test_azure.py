"""Azure OpenAI / Microsoft Foundry: settings detection, Azure credentials, ARM, Monitor and Cost Management."""

from __future__ import annotations

import json
import sys
import types
import urllib.parse
from datetime import datetime, timezone

import pytest

import usage_widget as w

UTC = timezone.utc
ARM = w.ARM
SUB = "00000000-1111-2222-3333-444444444444"
RID = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/my-res"
RID2 = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/Other"
ACCOUNTS = f"{ARM}/subscriptions/{SUB}/providers/Microsoft.CognitiveServices/accounts"


def write_claude_settings(home, env):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": env}), encoding="utf-8")


def write_codex_config(home, text):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# -- azure_settings ----------------------------------------------------------------

def test_settings_none_when_unused():
    assert w.azure_settings() is None


def test_settings_disabled(home):
    write_codex_config(home, 'base_url = "https://my-res.openai.azure.com/openai/v1"\n')
    w.save_config({"azure": False})
    assert w.azure_settings() is None


def test_settings_from_codex(home):
    write_codex_config(home, 'base_url = "https://my-res.openai.azure.com/openai/v1"\n'
                             'base_url = "https://my-res.openai.azure.com/other"\n'
                             'base_url = "https://found.services.ai.azure.com/models"\n')
    assert w.azure_settings() == {
        "subscription": "", "resources": ["my-res", "found"], "budget": "", "cost_management": True,
        "cost_interval": w.COST_INTERVAL, "metrics_interval": w.METRICS_INTERVAL,
    }


def test_settings_from_claude_foundry_resource(home):
    write_claude_settings(home, {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_RESOURCE": "claude-res",
                                 "AZURE_SUBSCRIPTION_ID": SUB})
    settings = w.azure_settings()
    assert settings["resources"] == ["claude-res"]
    assert settings["subscription"] == SUB


def test_settings_from_claude_foundry_base_url(home):
    write_claude_settings(home, {"CLAUDE_CODE_USE_FOUNDRY": "1",
                                 "ANTHROPIC_FOUNDRY_BASE_URL": "https://url-res.services.ai.azure.com/anthropic"})
    assert w.azure_settings()["resources"] == ["url-res"]


def test_settings_foundry_without_resource(home):
    write_claude_settings(home, {"CLAUDE_CODE_USE_FOUNDRY": "1"})
    assert w.azure_settings()["resources"] == []


def test_settings_config_overrides(home):
    write_codex_config(home, 'base_url = "https://my-res.openai.azure.com/openai/v1"\n')
    w.save_config({"azure": {"subscription": "cfg-sub", "resources": ["x", RID], "budget": "ai",
                             "cost_management": False, "cost_interval": 5, "metrics_interval": 300}})
    assert w.azure_settings() == {"subscription": "cfg-sub", "resources": ["x", RID], "budget": "ai",
                                  "cost_management": False, "cost_interval": w.MIN_INTERVAL,
                                  "metrics_interval": 300}


def test_settings_forced_on():
    w.save_config({"azure": True})
    assert w.azure_settings()["resources"] == []


# -- _azure_token ------------------------------------------------------------------

def test_token_from_az_cli(monkeypatch):
    calls = []
    out = json.dumps({"accessToken": "eyJ.az", "subscription": SUB, "expires_on": 4_000_000_000})
    monkeypatch.setattr(w, "run_cli", lambda args, timeout=30: calls.append(args) or out)
    assert w._azure_token() == ("eyJ.az", SUB)
    assert calls == [["az", "account", "get-access-token", "--resource", ARM + "/", "--output", "json"]]
    assert w._azure_token() == ("eyJ.az", SUB)  # cached
    assert len(calls) == 1


def test_token_expired_cache_is_refreshed(monkeypatch):
    outs = iter([json.dumps({"accessToken": "one", "expires_on": 1}), json.dumps({"accessToken": "two"})])
    monkeypatch.setattr(w, "run_cli", lambda args, timeout=30: next(outs))
    assert w._azure_token()[0] == "one"
    assert w._azure_token()[0] == "two"


def test_token_from_azure_identity(monkeypatch):
    monkeypatch.setattr(w, "run_cli", lambda args, timeout=30: "not json")
    scopes = []

    class Credential:
        def get_token(self, scope):
            scopes.append(scope)
            return types.SimpleNamespace(token="from-identity", expires_on=4_000_000_000)

    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = Credential
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    assert w._azure_token() == ("from-identity", "")
    assert scopes == [ARM + "/.default"]


def test_no_azure_credentials():
    with pytest.raises(w.AuthError, match="az login"):
        w._azure_token()


# -- ARM helpers -------------------------------------------------------------------

def test_arm_list_follows_next_link(fake_http):
    fake_http.responses[ARM + "/things"] = {"value": [1, 2], "nextLink": ARM + "/things-page2"}
    fake_http.responses[ARM + "/things-page2"] = {"value": [3]}
    assert w._arm_list({}, "/things") == [1, 2, 3]
    assert all(kw["auth_errors"] is False for kw in fake_http.kwargs)


def accounts(*items):
    return {"value": [
        {"id": f"/subscriptions/{SUB}/rg/{name}", "name": name, "kind": kind,
         "properties": {"customSubDomainName": sub} if sub else {}}
        for name, kind, sub in items
    ]}


def test_resource_ids_by_name_and_subdomain(fake_http):
    fake_http.responses[ACCOUNTS] = accounts(("My-Res", "OpenAI", None), ("acct2", "AIServices", "Sub-Domain"),
                                             ("unrelated", "OpenAI", None))
    ids = w._azure_resource_ids({}, SUB, ["my-res", "sub-domain", RID])
    assert ids == [RID, f"/subscriptions/{SUB}/rg/My-Res", f"/subscriptions/{SUB}/rg/acct2"]


def test_resource_ids_only_full_ids_skip_lookup(fake_http):
    assert w._azure_resource_ids({}, SUB, [RID, RID]) == [RID]
    assert fake_http.calls == []


def test_resource_ids_all_openai_accounts_in_every_subscription(fake_http):
    fake_http.responses[ARM + "/subscriptions?"] = {"value": [{"subscriptionId": "s1"}, {"subscriptionId": "s2"}]}
    fake_http.responses[f"{ARM}/subscriptions/s1/providers"] = {"value": [
        {"id": "/subscriptions/s1/a", "name": "a", "kind": "OpenAI"},
        {"id": "/subscriptions/s1/b", "name": "b", "kind": "Face"},
    ]}
    fake_http.responses[f"{ARM}/subscriptions/s2/providers"] = {"value": [
        {"id": "/subscriptions/s2/c", "name": "c", "kind": "AIServices"}]}
    assert w._azure_resource_ids({}, "", []) == ["/subscriptions/s1/a", "/subscriptions/s2/c"]


# -- Azure Monitor -----------------------------------------------------------------

def metrics(names, values_in, values_out):
    return {"value": [
        {"name": {"value": names[0]}, "timeseries": [{"data": [{"total": v} for v in values_in]}]},
        {"name": {"value": names[1]}, "timeseries": [{"data": [{"total": v} for v in values_out]} ]},
    ]}


def test_azure_tokens_openai_metrics(fake_http, frozen_now):
    frozen_now(datetime(2025, 2, 3, 10, 0, tzinfo=UTC))
    fake_http.responses[ARM + RID] = metrics(("ProcessedPromptTokens", "GeneratedTokens"), [100, None, 50], [7])
    since = datetime(2025, 2, 3, 0, 0, tzinfo=UTC)
    assert w._azure_tokens({}, RID, since) == {"in": 150, "out": 7}
    url = urllib.parse.urlsplit(fake_http.urls()[0])
    assert url.path == RID + "/providers/Microsoft.Insights/metrics"
    q = urllib.parse.parse_qs(url.query)
    assert q["metricnames"] == ["ProcessedPromptTokens,GeneratedTokens"]
    assert q["timespan"] == ["2025-02-03T00:00:00Z/2025-02-03T10:00:00Z"]
    assert q["aggregation"] == ["Total"]


def test_azure_tokens_falls_back_to_foundry_metrics(fake_http):
    fake_http.responses[ARM + RID] = [w.HTTPStatusError(400, "metric not supported"),
                                      metrics(("InputTokens", "OutputTokens"), [3], [4])]
    assert w._azure_tokens({}, RID, datetime(2025, 1, 1, tzinfo=UTC)) == {"in": 3, "out": 4}
    assert "metricnames=InputTokens%2COutputTokens" in fake_http.urls()[1]


def test_azure_tokens_neither_metric_set(fake_http):
    fake_http.responses[ARM + RID] = lambda url, kw: w.HTTPStatusError(400, "nope")
    with pytest.raises(w.HTTPStatusError):
        w._azure_tokens({}, RID, datetime(2025, 1, 1, tzinfo=UTC))
    assert len(fake_http.calls) == 2


def test_azure_tokens_other_errors_propagate(fake_http):
    fake_http.responses[ARM + RID] = w.HTTPStatusError(403, "denied")
    with pytest.raises(w.HTTPStatusError):
        w._azure_tokens({}, RID, datetime(2025, 1, 1, tzinfo=UTC))
    assert len(fake_http.calls) == 1


# -- Cost Management / Consumption -------------------------------------------------

def test_azure_month_cost(fake_http):
    fake_http.responses[f"{ARM}/subscriptions/{SUB}/providers/Microsoft.CostManagement/query"] = {"properties": {
        "columns": [{"name": "Cost", "type": "Number"}, {"name": "Currency", "type": "String"}],
        "rows": [[1.5, "EUR"], [2.25, "EUR"], [None, None]],
    }}
    assert w._azure_month_cost({}, SUB, [RID, RID2]) == {"amount": 3.75, "unit": "EUR"}
    body = fake_http.kwargs[0]["body"]
    assert body["timeframe"] == "MonthToDate"
    assert body["dataset"]["filter"]["dimensions"]["values"] == [RID.lower(), RID2.lower()]


def test_azure_month_cost_empty(fake_http):
    fake_http.responses[f"{ARM}/subscriptions/{SUB}/providers/Microsoft.CostManagement/query"] = {}
    assert w._azure_month_cost({}, SUB, [RID]) == {"amount": 0.0, "unit": "USD"}


def test_azure_budget_by_name(fake_http):
    fake_http.responses[f"{ARM}/subscriptions/{SUB}/providers/Microsoft.Consumption/budgets/ai%20monthly"] = {
        "properties": {"amount": 200, "timeGrain": "BillingMonth", "currentSpend": {"amount": 150, "unit": "USD"}}}
    assert w._azure_budget({}, SUB, "ai monthly") == {"spent": 150.0, "limit": 200.0, "unit": "USD",
                                                       "time_unit": "MONTHLY"}


def test_azure_budget_by_id(fake_http):
    bid = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Consumption/budgets/b"
    fake_http.responses[ARM + bid] = {"properties": {"amount": 10, "timeGrain": "Weird"}}
    assert w._azure_budget({}, "ignored", bid) == {"spent": 0.0, "limit": 10.0, "unit": "USD", "time_unit": None}


@pytest.mark.parametrize("grain, unit", [("Monthly", "MONTHLY"), ("Quarterly", "QUARTERLY"),
                                         ("BillingQuarter", "QUARTERLY"), ("Annually", "ANNUALLY"),
                                         ("BillingAnnual", "ANNUALLY")])
def test_azure_time_grains(grain, unit):
    assert w.AZURE_TIME_GRAINS[grain] == unit
    assert w._next_period(unit) is not None


# -- fetch_azure -------------------------------------------------------------------

@pytest.fixture
def az_login(monkeypatch, fake_http):
    out = json.dumps({"accessToken": "eyJ.tok", "subscription": SUB, "expires_on": 4_000_000_000})
    monkeypatch.setattr(w, "run_cli", lambda args, timeout=30: out if args[0] == "az" else None)
    return fake_http


def healthy(fake_http):
    fake_http.responses[ACCOUNTS] = {"value": [
        {"id": RID, "name": "my-res", "kind": "OpenAI"}, {"id": RID2, "name": "Other", "kind": "AIServices"}]}
    fake_http.responses[f"{ARM}/subscriptions/{SUB}/providers/Microsoft.CostManagement/query"] = {"properties": {
        "columns": [{"name": "Cost", "type": "Number"}, {"name": "Currency", "type": "String"}],
        "rows": [[42.0, "USD"]]}}
    fake_http.responses[f"{ARM}/subscriptions/{SUB}/providers/Microsoft.Consumption/budgets/"] = {
        "properties": {"amount": 50, "timeGrain": "Monthly", "currentSpend": {"amount": 46, "unit": "USD"}}}
    fake_http.responses[ARM + RID + "/providers/Microsoft.Insights"] = metrics(
        ("ProcessedPromptTokens", "GeneratedTokens"), [1000], [200])
    fake_http.responses[ARM + RID2 + "/providers/Microsoft.Insights"] = metrics(
        ("ProcessedPromptTokens", "GeneratedTokens"), [500], [300])


def test_fetch_azure_not_used():
    with pytest.raises(w.NotUsed):
        w.fetch_azure()


def test_fetch_azure_no_credentials():
    w.save_config({"azure": True})
    with pytest.raises(w.AuthError, match="no Azure credentials"):
        w.fetch_azure()


def test_fetch_azure_full(az_login):
    healthy(az_login)
    w.save_config({"azure": {"resources": ["my-res", "other"], "budget": "ai"}})
    usage = w.fetch_azure()
    assert usage.name == "Azure"
    budget, month, tokens = usage.limits
    assert budget.label == "Budget $46.00 / $50.00"
    assert budget.percent == 92.0
    assert budget.level() == "critical"
    assert (month.label, month.percent) == ("This month $42.00", None)
    assert tokens.label == "Tokens today 1.5k in · 500 out"
    assert usage.summary == "$42.00"
    assert w.summary_text([usage]) == "Azure 92%"
    assert usage.account.endswith(":my-res,other")  # subscription and resources: a synced cutoff applies to them only
    assert all(headers == {"Authorization": "Bearer eyJ.tok"} for _, headers in az_login.calls)


def test_fetch_azure_caches(az_login):
    healthy(az_login)
    w.save_config({"azure": {"resources": ["my-res"]}})
    w.fetch_azure()
    n = len(az_login.calls)
    w.fetch_azure()
    assert len(az_login.calls) == n


def test_fetch_azure_tokens_only(az_login):
    healthy(az_login)
    w.save_config({"azure": {"resources": [RID], "cost_management": False}})
    usage = w.fetch_azure()
    assert [lim.label for lim in usage.limits] == ["Tokens today 1.0k in · 200 out"]
    assert usage.summary == "1.2k tok"


def test_fetch_azure_costs_per_subscription(az_login):
    other = "/subscriptions/sub-b/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/b"
    az_login.responses[f"{ARM}/subscriptions/{SUB}/providers/Microsoft.CostManagement/query"] = {"properties": {
        "columns": [{"name": "Cost", "type": "Number"}], "rows": [[1.0]]}}
    az_login.responses[f"{ARM}/subscriptions/sub-b/providers/Microsoft.CostManagement/query"] = {"properties": {
        "columns": [{"name": "Cost", "type": "Number"}], "rows": [[2.0]]}}
    az_login.responses[ARM + RID + "/providers"] = metrics(("ProcessedPromptTokens", "GeneratedTokens"), [], [])
    az_login.responses[ARM + other + "/providers"] = metrics(("ProcessedPromptTokens", "GeneratedTokens"), [], [])
    w.save_config({"azure": {"resources": [RID, other]}})
    usage = w.fetch_azure()
    assert usage.summary == "$3.00"


def test_fetch_azure_no_resources_found(az_login):
    az_login.responses[ACCOUNTS] = {"value": []}
    w.save_config({"azure": {"resources": ["missing"]}})
    usage = w.fetch_azure()
    assert [lim.label for lim in usage.limits] == ['⚠ no Azure OpenAI resource found — set "resources" in config.json']


def test_fetch_azure_resource_listing_denied(az_login):
    az_login.responses[ACCOUNTS] = w.HTTPStatusError(403, "AuthorizationFailed")
    w.save_config({"azure": {"resources": ["my-res"]}})
    usage = w.fetch_azure()
    assert [lim.label for lim in usage.limits] == ["⚠ resources: HTTP 403: AuthorizationFailed"]


def test_fetch_azure_expired_login(az_login):
    az_login.responses[ACCOUNTS] = w.HTTPStatusError(401, "ExpiredAuthenticationToken")
    w.save_config({"azure": {"resources": ["my-res"]}})
    with pytest.raises(w.AuthError, match="Azure login expired"):
        w.fetch_azure()


def test_fetch_azure_budget_needs_a_subscription(monkeypatch, fake_http):
    out = json.dumps({"accessToken": "t"})  # no subscription from the CLI either
    monkeypatch.setattr(w, "run_cli", lambda args, timeout=30: out)
    w.save_config({"azure": {"resources": [], "budget": "ai", "cost_management": False}})
    fake_http.responses[ARM + "/subscriptions?"] = {"value": []}
    usage = w.fetch_azure()
    assert not any(lim.label.startswith("Budget") for lim in usage.limits)
    assert not fake_http.urls(ARM + "/subscriptions//")
