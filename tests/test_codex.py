"""Codex: the Codex CLI's ChatGPT login, Bedrock detection and the usage response."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import usage_widget as w

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def write_auth(home, data=None, raw: str | None = None):
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")


def write_config(home, text: str):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def logged_in(home):
    write_auth(home, {"tokens": {"access_token": "acc", "account_id": "acct-1"}})


# -- login ---------------------------------------------------------------------

def test_tokens(logged_in):
    assert w._codex_tokens() == {"access_token": "acc", "account_id": "acct-1"}


def test_honours_codex_home(monkeypatch, tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": "x"}}))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert w._codex_tokens()["access_token"] == "x"


def test_not_logged_in():
    with pytest.raises(w.AuthError, match="codex login"):
        w._codex_tokens()


def test_unreadable_auth_file(home):
    write_auth(home, raw="{nope")
    with pytest.raises(w.AuthError, match="cannot read"):
        w._codex_tokens()


@pytest.mark.parametrize("data", [{}, {"tokens": None}, {"tokens": {"access_token": ""}}])
def test_auth_file_without_token(home, data):
    write_auth(home, data)
    with pytest.raises(w.AuthError, match="not logged in"):
        w._codex_tokens()


def test_api_key_login_has_no_plan(home):
    write_auth(home, {"OPENAI_API_KEY": "sk-..."})
    with pytest.raises(w.AuthError, match="API-key login"):
        w._codex_tokens()


def test_bedrock_user_without_login_is_left_out(home):
    write_config(home, 'base_url = "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1"\n')
    with pytest.raises(w.NotUsed):
        w._codex_tokens()


def test_bedrock_user_with_api_key_is_left_out(home):
    write_config(home, 'base_url = "https://bedrock-mantle.us-east-1.api.aws/v1"\n')
    write_auth(home, {"OPENAI_API_KEY": "sk-..."})
    with pytest.raises(w.NotUsed):
        w._codex_tokens()


# -- config.toml ---------------------------------------------------------------

def test_codex_bedrock_urls(home):
    assert w.codex_bedrock_urls() == []
    write_config(home, """
model = "gpt-5"
base_url = "https://api.openai.com/v1"

[model_providers.bedrock]
  base_url = "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1"

[model_providers.mantle]
base_url='https://bedrock-mantle.eu-central-1.api.aws/v1'
# base_url = "https://bedrock-runtime.ap-south-1.amazonaws.com"  (commented out)
""")
    assert w.codex_bedrock_urls() == [
        "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1",
        "https://bedrock-mantle.eu-central-1.api.aws/v1",
    ]


# -- fetch_codex ---------------------------------------------------------------

def test_request_headers(logged_in, fake_http):
    fake_http.responses[USAGE_URL] = {}
    w.fetch_codex()
    url, headers = fake_http.calls[0]
    assert url == USAGE_URL
    assert headers == {"Authorization": "Bearer acc", "ChatGPT-Account-Id": "acct-1"}


def test_no_account_header_without_account_id(home, fake_http):
    write_auth(home, {"tokens": {"access_token": "acc"}})
    fake_http.responses[USAGE_URL] = {}
    assert w.fetch_codex().account == ""
    assert "ChatGPT-Account-Id" not in fake_http.calls[0][1]


def test_usage_names_the_account(logged_in, fake_http):
    fake_http.responses[USAGE_URL] = {}
    assert w.fetch_codex().account == "acct-1"  # so a synced cutoff only applies to this account


def test_full_response(logged_in, fake_http):
    fake_http.responses[USAGE_URL] = {
        "plan_type": "plus",
        "rate_limit": {
            "limit_reached": False,
            "primary_window": {"used_percent": 13, "limit_window_seconds": 18000, "reset_at": 1_900_000_000},
            "secondary_window": {"used_percent": 40.5, "limit_window_seconds": 604800},
        },
        "code_review_rate_limit": {
            "primary_window": {"used_percent": 2, "limit_window_seconds": 604800},
            "secondary_window": None,
        },
        "additional_rate_limits": [
            {"limit_name": "GPT-5 Pro", "rate_limit": {"primary_window": {"used_percent": 7,
                                                                           "limit_window_seconds": 86400}}},
            {"metered_feature": "cloud_tasks", "primary_window": {"used_percent": 1, "limit_window_seconds": 3600}},
            {"primary_window": {"used_percent": 3}},
            "garbage",
        ],
        "credits": {"has_credits": True, "unlimited": False, "balance": "42"},
    }
    usage = w.fetch_codex()
    assert usage.name == "Codex"
    assert usage.plan == "plus"
    assert [(lim.label, lim.percent) for lim in usage.limits] == [
        ("5h", 13.0), ("Weekly", 40.5), ("Code review · Weekly", 2.0), ("GPT-5 Pro · 1d", 7.0),
        ("cloud_tasks · 1h", 1.0), ("Other · Limit", 3.0), ("Credits: 42", 0.0),
    ]
    assert usage.limits[0].resets_at == datetime.fromtimestamp(1_900_000_000, tz=timezone.utc)
    assert usage.limits[1].resets_at is None
    assert usage.credits == w.Credits(left=42.0, what="credits left")


def test_limit_reached_marks_full_windows_critical(logged_in, fake_http):
    fake_http.responses[USAGE_URL] = {"rate_limit": {
        "limit_reached": True,
        "primary_window": {"used_percent": 100, "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 30, "limit_window_seconds": 604800},
    }}
    primary, secondary = w.fetch_codex().limits
    assert primary.severity == "critical"
    assert secondary.severity == ""
    assert secondary.level() == "normal"


@pytest.mark.parametrize("credits", [None, {}, {"has_credits": False, "balance": "5"},
                                     {"has_credits": True, "unlimited": True, "balance": "5"}])
def test_credits_hidden(logged_in, fake_http, credits):
    fake_http.responses[USAGE_URL] = {"credits": credits}
    usage = w.fetch_codex()
    assert usage.limits == []
    assert usage.credits is None


@pytest.mark.parametrize("balance, left", [("42", 42.0), ("12.5", 12.5), (7, 7.0), (None, 0.0)])
def test_credit_balance_detected(logged_in, fake_http, balance, left):
    credits = {"has_credits": True, "unlimited": False}
    if balance is not None:
        credits["balance"] = balance
    fake_http.responses[USAGE_URL] = {"credits": credits}
    assert w.fetch_codex().credits == w.Credits(left=left, what="credits left")


def test_unreadable_credit_balance_shows_but_sets_no_credits(logged_in, fake_http):
    fake_http.responses[USAGE_URL] = {"credits": {"has_credits": True, "unlimited": False, "balance": "lots"}}
    usage = w.fetch_codex()
    assert [lim.label for lim in usage.limits] == ["Credits: lots"]
    assert usage.credits is None


@pytest.mark.parametrize("rate_limit", [None, "x", [], {"primary_window": "x"}])
def test_malformed_rate_limits_are_skipped(logged_in, fake_http, rate_limit):
    fake_http.responses[USAGE_URL] = {"rate_limit": rate_limit, "additional_rate_limits": {"not": "a list"}}
    assert w.fetch_codex().limits == []


CLOUD_CONFIG = """
base_url = "https://api.openai.com/v1"
base_url = "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1"
base_url = "https://us-central1-aiplatform.googleapis.com/v1/projects/my-proj/locations/us-central1/endpoints/openapi"
base_url = "https://my-res.openai.azure.com/openai/v1"
base_url = "https://Other-Res.cognitiveservices.azure.com/openai"
base_url = "https://found-1.services.ai.azure.com/models"
base_url = "https://evil.example.com/?x=openai.azure.com"
"""


def test_codex_url_classifiers(home):
    write_config(home, CLOUD_CONFIG)
    assert len(w.codex_base_urls()) == 7
    assert w.codex_bedrock_urls() == ["https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1"]
    assert w.codex_vertex_urls() == [
        "https://us-central1-aiplatform.googleapis.com/v1/projects/my-proj/locations/us-central1/endpoints/openapi"]
    assert w.codex_azure_urls() == ["https://my-res.openai.azure.com/openai/v1",
                                    "https://Other-Res.cognitiveservices.azure.com/openai",
                                    "https://found-1.services.ai.azure.com/models"]
    assert len(w.codex_cloud_urls()) == 5


@pytest.mark.parametrize("url", ["https://my-res.openai.azure.com/openai/v1",
                                 "https://us-central1-aiplatform.googleapis.com/v1/projects/p/x"])
def test_cloud_user_without_login_is_left_out(home, url):
    write_config(home, f'base_url = "{url}"\n')
    with pytest.raises(w.NotUsed):
        w._codex_tokens()


def test_plain_openai_base_url_still_needs_login(home):
    write_config(home, 'base_url = "https://api.openai.com/v1"\n')
    with pytest.raises(w.AuthError):
        w._codex_tokens()
