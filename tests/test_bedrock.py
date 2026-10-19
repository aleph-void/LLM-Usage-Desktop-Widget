"""Amazon Bedrock: settings detection, the on-disk cache, the AWS queries and fetch_bedrock."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import pytest

import usage_widget as w

boto3 = pytest.importorskip("boto3")
from botocore.exceptions import (  # noqa: E402
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ProfileNotFound,
    SSOTokenLoadError,
    TokenRetrievalError,
)

UTC = timezone.utc


def write_claude_settings(home, env):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": env}), encoding="utf-8")


def write_codex_config(home, text):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# -- bedrock_settings ----------------------------------------------------------

def test_settings_none_when_unused():
    assert w.bedrock_settings() is None


def test_settings_disabled_in_config(home):
    write_claude_settings(home, {"CLAUDE_CODE_USE_BEDROCK": "1"})
    w.save_config({"bedrock": False})
    assert w.bedrock_settings() is None


def test_settings_from_claude_code(home):
    write_claude_settings(home, {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-west-2", "AWS_PROFILE": "work"})
    assert w.bedrock_settings() == {
        "profile": "work", "regions": ["us-west-2"], "budget": "", "cost_explorer": True,
        "cost_interval": w.COST_INTERVAL, "metrics_interval": w.METRICS_INTERVAL,
    }


def test_settings_claude_default_region_fallback(home):
    write_claude_settings(home, {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_DEFAULT_REGION": "eu-west-1"})
    assert w.bedrock_settings()["regions"] == ["eu-west-1"]


def test_settings_from_codex_urls(home):
    write_codex_config(home, 'base_url = "https://bedrock-runtime.us-east-1.amazonaws.com/v1"\n'
                             'base_url = "https://bedrock-mantle.us-gov-west-1.api.aws/v1"\n'
                             'base_url = "https://bedrock.example.internal/v1"\n'
                             'base_url = "https://bedrock-runtime.us-east-1.amazonaws.com/other"\n')
    settings = w.bedrock_settings()
    assert settings["regions"] == ["us-east-1", "us-gov-west-1"]  # deduplicated, region-less URL dropped
    assert settings["profile"] is None


def test_settings_forced_on_without_regions():
    w.save_config({"bedrock": True})
    settings = w.bedrock_settings()
    assert settings["regions"] == []
    assert settings["cost_explorer"] is True


def test_settings_config_overrides(home):
    write_claude_settings(home, {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-west-2", "AWS_PROFILE": "env"})
    w.save_config({"bedrock": {"profile": "cfg", "regions": ["ap-south-1", "ap-south-1"], "budget": "b",
                               "cost_explorer": False, "cost_interval": 5, "metrics_interval": 120}})
    assert w.bedrock_settings() == {
        "profile": "cfg", "regions": ["ap-south-1"], "budget": "b", "cost_explorer": False,
        "cost_interval": w.MIN_INTERVAL, "metrics_interval": 120,
    }


def test_settings_dict_config_enables_without_detection():
    w.save_config({"bedrock": {"regions": ["us-east-2"]}})
    assert w.bedrock_settings()["regions"] == ["us-east-2"]


# -- _cached -------------------------------------------------------------------

def test_cached_computes_once_and_persists(monkeypatch):
    calls = []
    compute = lambda: calls.append(1) or {"n": len(calls)}  # noqa: E731
    assert w._cached("k", 100, compute) == {"n": 1}
    assert w._cached("k", 100, compute) == {"n": 1}
    assert len(calls) == 1
    stored = json.loads((w.config_dir() / "cloud-cache.json").read_text())
    assert stored["k"]["value"] == {"n": 1}


def test_cached_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(w.time, "time", lambda: clock[0])
    values = iter([1, 2])
    assert w._cached("k", 60, lambda: next(values)) == 1
    clock[0] += 59
    assert w._cached("k", 60, lambda: next(values)) == 1
    clock[0] += 2
    assert w._cached("k", 60, lambda: next(values)) == 2


def test_cached_ignores_entries_from_the_future(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(w.time, "time", lambda: clock[0])
    w._cached("k", 60, lambda: "old")
    clock[0] = 500.0  # clock went backwards
    assert w._cached("k", 60, lambda: "new") == "new"


def test_cached_prunes_week_old_entries(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(w.time, "time", lambda: clock[0])
    w._cached("ancient", 60, lambda: 1)
    clock[0] = 8 * 86400
    w._cached("fresh", 60, lambda: 2)
    stored = json.loads((w.config_dir() / "cloud-cache.json").read_text())
    assert set(stored) == {"fresh"}


def test_cached_survives_corrupt_file():
    path = w.config_dir() / "cloud-cache.json"
    path.parent.mkdir(parents=True)
    path.write_text("{corrupt")
    assert w._cached("k", 60, lambda: 7) == 7


def test_cached_propagates_errors_and_stores_nothing():
    with pytest.raises(ValueError):
        w._cached("k", 60, lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert w._cached("k", 60, lambda: "ok") == "ok"


# -- fake AWS ------------------------------------------------------------------

class FakeClient:
    def __init__(self, **methods):
        self.__dict__.update(methods)


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages
        self.kwargs = None

    def paginate(self, **kwargs):
        self.kwargs = kwargs
        return iter(self.pages)


class FakeSession:
    def __init__(self, clients=None, credentials=True, region_name="us-east-1"):
        self.clients = clients or {}
        self.credentials = credentials
        self.region_name = region_name
        self.requested: list[tuple[str, str | None]] = []

    def client(self, name, region_name=None):
        self.requested.append((name, region_name))
        factory = self.clients[name]
        return factory(region_name) if callable(factory) and not isinstance(factory, FakeClient) else factory

    def get_credentials(self):
        return object() if self.credentials else None


def budget_clients(budget):
    return {
        "sts": FakeClient(get_caller_identity=lambda: {"Account": "123456789012"}),
        "budgets": FakeClient(describe_budget=lambda AccountId, BudgetName: {"Budget": budget}),
    }


def ce_client(pages):
    requests = []

    def get_cost_and_usage(**request):
        requests.append(json.loads(json.dumps(request)))
        return pages[len(requests) - 1]

    return FakeClient(get_cost_and_usage=get_cost_and_usage, requests=requests)


def cloudwatch_client(pages):
    paginator = FakePaginator(pages)
    return FakeClient(get_paginator=lambda name: paginator, paginator=paginator)


def metric_pages(rin, rout, min_, mout):
    return [{"MetricDataResults": [
        {"Id": "runtime_in", "Values": rin}, {"Id": "runtime_out", "Values": rout},
    ]}, {"MetricDataResults": [
        {"Id": "mantle_in", "Values": min_}, {"Id": "mantle_out", "Values": mout},
    ]}]


# -- AWS queries ---------------------------------------------------------------

def test_bedrock_budget():
    session = FakeSession(budget_clients({
        "BudgetLimit": {"Amount": "100.0", "Unit": "USD"}, "TimeUnit": "MONTHLY",
        "CalculatedSpend": {"ActualSpend": {"Amount": "42.5", "Unit": "USD"}},
    }))
    assert w._bedrock_budget(session, "b") == {"spent": 42.5, "limit": 100.0, "unit": "USD", "time_unit": "MONTHLY"}
    assert ("budgets", "us-east-1") in session.requested


def test_bedrock_budget_without_spend():
    session = FakeSession(budget_clients({"BudgetLimit": {"Amount": "10"}}))
    assert w._bedrock_budget(session, "b") == {"spent": 0.0, "limit": 10.0, "unit": "USD", "time_unit": None}


def test_bedrock_month_cost_filters_and_paginates(frozen_now):
    frozen_now(datetime(2025, 3, 31, 23, 0, tzinfo=UTC))
    ce = ce_client([
        {"ResultsByTime": [{"Groups": [
            {"Keys": ["Amazon Bedrock"], "Metrics": {"UnblendedCost": {"Amount": "1.25", "Unit": "USD"}}},
            {"Keys": ["Amazon EC2"], "Metrics": {"UnblendedCost": {"Amount": "99", "Unit": "USD"}}},
        ]}], "NextPageToken": "p2"},
        {"ResultsByTime": [{"Groups": [
            {"Keys": ["Claude Sonnet 4.5 (Amazon Bedrock Edition)"],
             "Metrics": {"UnblendedCost": {"Amount": "3.75", "Unit": "USD"}}},
        ]}]},
    ])
    session = FakeSession({"ce": ce})
    assert w._bedrock_month_cost(session) == {"amount": 5.0, "unit": "USD"}
    first, second = ce.requests
    assert first["TimePeriod"] == {"Start": "2025-03-01", "End": "2025-04-01"}
    assert "NextPageToken" not in first
    assert second["NextPageToken"] == "p2"
    assert ("ce", "us-east-1") in session.requested


def test_bedrock_month_cost_empty():
    session = FakeSession({"ce": ce_client([{}])})
    assert w._bedrock_month_cost(session) == {"amount": 0.0, "unit": "USD"}


def test_bedrock_tokens_sums_runtime_and_mantle():
    cw = cloudwatch_client(metric_pages([100, 50], [10], [1000], [5, 5]))
    session = FakeSession({"cloudwatch": cw})
    since = datetime(2025, 1, 1, tzinfo=UTC)
    assert w._bedrock_tokens(session, "us-west-2", since) == {"in": 1150.0, "out": 20.0}
    assert session.requested == [("cloudwatch", "us-west-2")]
    assert cw.paginator.kwargs["StartTime"] == since
    ids = [q["Id"] for q in cw.paginator.kwargs["MetricDataQueries"]]
    assert ids == ["runtime_in", "runtime_out", "mantle_in", "mantle_out"]
    assert 'MetricName="InputTokenCount"' in cw.paginator.kwargs["MetricDataQueries"][0]["Expression"]


# -- fetch_bedrock -------------------------------------------------------------

@pytest.fixture
def bedrock_on():
    w.save_config({"bedrock": {"regions": ["us-east-1"]}})


@pytest.fixture
def use_session(monkeypatch):
    def install(session=None, error=None):
        seen = {}

        def factory(profile_name=None):
            seen["profile"] = profile_name
            if error:
                raise error
            return session

        monkeypatch.setattr(boto3, "Session", factory)
        return seen

    return install


def healthy_session(cost="12.34", tokens=None):
    pages = tokens if tokens is not None else metric_pages([1500], [500], [0], [0])
    return FakeSession({
        **budget_clients({"BudgetLimit": {"Amount": "50", "Unit": "USD"}, "TimeUnit": "MONTHLY",
                          "CalculatedSpend": {"ActualSpend": {"Amount": "40"}}}),
        "ce": ce_client([{"ResultsByTime": [{"Groups": [
            {"Keys": ["Amazon Bedrock"], "Metrics": {"UnblendedCost": {"Amount": cost, "Unit": "USD"}}}]}]}]),
        "cloudwatch": lambda region: cloudwatch_client(pages),
    })


def test_fetch_bedrock_not_used():
    with pytest.raises(w.NotUsed):
        w.fetch_bedrock()


def test_fetch_bedrock_needs_boto3(monkeypatch, bedrock_on):
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(RuntimeError, match="needs boto3"):
        w.fetch_bedrock()


def test_fetch_bedrock_full(bedrock_on, use_session):
    w.save_config({"bedrock": {"regions": ["us-east-1", "us-west-2"], "budget": "monthly", "profile": "work"}})
    seen = use_session(healthy_session())
    usage = w.fetch_bedrock()
    assert seen["profile"] == "work"
    assert usage.name == "Bedrock"
    assert usage.error == ""
    assert usage.summary == "$12.34"
    budget, month, tokens = usage.limits
    assert budget.label == "Budget $40.00 / $50.00"
    assert budget.percent == 80.0
    assert budget.level() == "warning"
    assert budget.resets_at is not None and budget.resets_at.day == 1
    assert (month.label, month.percent) == ("This month $12.34", None)
    assert tokens.label == "Tokens today 3.0k in · 1.0k out"  # two regions summed
    assert w.top_limit(usage) is budget


def test_fetch_bedrock_uses_cache(bedrock_on, use_session):
    use_session(healthy_session(cost="1"))
    first = w.fetch_bedrock()
    use_session(healthy_session(cost="999"))
    assert w.fetch_bedrock().summary == first.summary == "$1.00"


def test_fetch_bedrock_token_summary_without_cost_explorer(use_session):
    w.save_config({"bedrock": {"regions": ["eu-west-1"], "cost_explorer": False}})
    use_session(healthy_session(tokens=metric_pages([2_000_000], [500_000], [], [])))
    usage = w.fetch_bedrock()
    assert usage.summary == "2.5M tok"
    assert [lim.label for lim in usage.limits] == ["Tokens today 2.0M in · 500.0k out"]


def test_fetch_bedrock_zero_budget_limit(use_session):
    w.save_config({"bedrock": {"regions": ["us-east-1"], "budget": "b", "cost_explorer": False}})
    session = healthy_session()
    session.clients.update(budget_clients({"BudgetLimit": {"Amount": "0"}, "TimeUnit": "DAILY"}))
    use_session(session)
    assert w.fetch_bedrock().limits[0].percent == 0.0


def test_fetch_bedrock_falls_back_to_session_region(use_session):
    w.save_config({"bedrock": True})
    session = healthy_session()
    session.region_name = "ca-central-1"
    use_session(session)
    w.fetch_bedrock()
    assert ("cloudwatch", "ca-central-1") in session.requested


def test_fetch_bedrock_default_region(use_session):
    w.save_config({"bedrock": True})
    session = healthy_session()
    session.region_name = None
    use_session(session)
    w.fetch_bedrock()
    assert ("cloudwatch", "us-east-1") in session.requested


def test_fetch_bedrock_profile_not_found(bedrock_on, use_session):
    w.save_config({"bedrock": {"profile": "ghost"}})
    use_session(error=ProfileNotFound(profile="ghost"))
    with pytest.raises(w.AuthError, match="'ghost' not found"):
        w.fetch_bedrock()


def test_fetch_bedrock_no_credentials(bedrock_on, use_session):
    use_session(FakeSession(credentials=False))
    with pytest.raises(w.AuthError, match="aws sso login"):
        w.fetch_bedrock()


def failing_session(error):
    def boom(*a, **k):
        raise error

    return FakeSession({"ce": FakeClient(get_cost_and_usage=boom),
                        "cloudwatch": lambda region: FakeClient(get_paginator=boom)})


@pytest.mark.parametrize("error, match", [
    (NoCredentialsError(), "no AWS credentials"),
    (ClientError({"Error": {"Code": "ExpiredToken"}}, "GetCostAndUsage"), "expired"),
    (ClientError({"Error": {"Code": "UnrecognizedClientException"}}, "GetCostAndUsage"), "expired"),
    (TokenRetrievalError(provider="sso", error_msg="x"), "SSO session expired"),
    (SSOTokenLoadError(error_msg="x"), "SSO session expired"),
])
def test_fetch_bedrock_credential_errors_fail_the_provider(bedrock_on, use_session, error, match):
    w.save_config({"bedrock": {"regions": ["us-east-1"], "profile": "dev"}})
    use_session(failing_session(error))
    with pytest.raises(w.AuthError, match=match) as info:
        w.fetch_bedrock()
    if "expired" in match or "credentials" in match:
        assert "--profile dev" in str(info.value)


def test_fetch_bedrock_other_errors_become_notes(bedrock_on, use_session):
    session = FakeSession({
        "ce": FakeClient(get_cost_and_usage=lambda **k: (_ for _ in ()).throw(
            ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetCostAndUsage"))),
        "cloudwatch": lambda region: FakeClient(get_paginator=lambda n: (_ for _ in ()).throw(
            EndpointConnectionError(endpoint_url="https://monitoring"))),
    })
    use_session(session)
    usage = w.fetch_bedrock()
    labels = [lim.label for lim in usage.limits]
    assert labels[0] == "⚠ Cost Explorer: AccessDeniedException"
    assert labels[1].startswith("⚠ CloudWatch us-east-1: ")
    assert all(lim.percent is None for lim in usage.limits)
    assert usage.summary == ""
