"""S3: raw secrets must not sit in a connection's settings."""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from thallo.connections import InvalidConnection
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.models import Connection  # noqa: F401

from .conftest import T1
from .test_registry import TEXT, Hosted, S, register


@pytest.mark.parametrize("settings", [
    {"key": "x"}, {"access_key": "x"}, {"signing_key": "x"}, {"dsn": "x"}, {"webhook": "x"}, {"apiKey": "x"},
    {"API_KEY": "x"}, {"client-secret": "x"}, {"nested": [{"password": "x"}]}, {"webhook_url": "x"},
])
def test_secretish_key_names_refused(registry, providers, settings):
    register(providers, "a")
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "a", settings=settings)


@pytest.mark.parametrize("value", [
    "Bearer abcdefghijklmnop", "sk_" + "live_abcdefghijkl", "sk-ant-abcdefghijkl", "AKIA" + "ABCDEFGHIJKLMNOP",
    "postgres://user:pass@host/db", "https://hooks.slack.com/services/T000/B000/XXXXXXXX",
    "https://hooks.zapier.com/hooks/catch/123456/abcdef/", "https://discord.com/api/webhooks/1/abc",
    "Zx9fQ2mK7pL4vB8nC3dR6tY1wE5uI0oA",
])
def test_secretish_values_refused_under_any_key(registry, providers, value):
    register(providers, "a")
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "a", settings={"endpoint": value})
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "a", settings={"deep": {"list": [value]}})


@pytest.mark.parametrize("settings", [
    {"endpoint": "https://api.example.invalid/v1/chat/completions"}, {"region": "eu-west-1"},
    {"keywords": ["finance"], "monkey": 1}, {"model_family": "claude"}, {"max_tokens_per_day": 1000},
    {"note": "a perfectly ordinary sentence that is long enough but has spaces in it"},
    {"ref_hint": "sec_abc"},
])
def test_ordinary_settings_pass(registry, providers, settings):
    register(providers, "a")
    registry.add(T1, TEXT, "a", settings=settings)


# Every name any earlier version of the detector caught. This list may only grow.
EVER_CAUGHT = [
    "secret", "secrets", "token", "tokens", "password", "passwd", "pwd", "key", "apikey", "accesskey", "privatekey",
    "signingkey", "credential", "credentials", "authorization", "auth", "bearer", "dsn", "webhook", "webhooks",
    "signature", "cookie", "passphrase", "api_key", "private_key", "access_key", "signing_key", "client_secret",
    "auth_token", "db_password", "x-api-key", "Authorization", "session_cookie", "gpg_passphrase",
    "request_signature", "webhook_url",
]


@pytest.mark.parametrize("name", EVER_CAUGHT)
def test_no_name_ever_caught_is_dropped(registry, providers, name):
    register(providers, "a")
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "a", settings={name: "x"})


@pytest.mark.parametrize("name", ["max_tokens_secret", "tokensecret", "maxtokenspassword", "max_tokens_per_day_key",
                                  "tokenlimitsecret", "outputtokensapikey"])
def test_a_benign_word_cannot_hide_part_of_a_secret_stem(registry, providers, name):
    register(providers, "a")
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "a", settings={name: "x"})


VAULT_REF = "sec_" + "0123456789abcdef" * 2


@pytest.mark.parametrize("settings", [
    {"api_key_ref": "sk_" + "live_abcdefghijkl"}, {"anything_hint": "Bearer abcdefghijklmnop"},
    {"token_ref": VAULT_REF}, {"api_key_ref": VAULT_REF}, {"secret_ref": "sk_" + "live_abcdefghijkl"},
    {"secret_ref": "not-a-vault-ref"}, {"secret_ref": {"nested": "Bearer abcdefghijklmnop"}},
    {"label_ref": "AKIA" + "ABCDEFGHIJKLMNOP"},
])
def test_ref_and_hint_names_are_not_an_exemption(registry, providers, settings):
    register(providers, "a")
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "a", settings=settings)


def test_only_a_vault_shaped_secret_ref_is_exempt(registry, providers):
    register(providers, "a")
    registry.add(T1, TEXT, "a", settings={"secret_ref": VAULT_REF})


@pytest.mark.parametrize("name", [
    "mysecretvalue", "accesstoken", "dbpassword", "MySecretValue", "clientcredentials", "xapikeyx", "myprivatekey",
    "dbpasswd", "bearerheader", "authorizationheader", "slackwebhookurl", "accesstokens", "signingkey",
])
def test_strong_stems_match_inside_a_name(registry, providers, name):
    register(providers, "a")
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "a", settings={name: "x"})


@pytest.mark.parametrize("name", ["author", "authority", "keywords", "monkey", "keyboard", "dsnfoo", "pwdx",
                                  "max_tokens_per_day", "max_output_tokens", "token_limit", "turkey"])
def test_short_ambiguous_stems_match_whole_tokens_only(registry, providers, name):
    register(providers, "a")
    registry.add(T1, TEXT, "a", settings={name: 1})


@pytest.mark.parametrize("name", ["key", "auth", "pwd", "dsn", "db_dsn", "my_key", "authKey", "pwd_x"])
def test_short_ambiguous_stems_match_as_a_token(registry, providers, name):
    register(providers, "a")
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "a", settings={name: "x"})


def test_declared_settings_schema_refuses_unknown_keys(registry, providers):
    class Schema(BaseModel):
        model_config = ConfigDict(extra="forbid")
        endpoint: str
        region: str = "eu"

    providers.register(TEXT, "typed", ConnectionMode.HOSTED, Hosted, settings_schema=Schema)
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "typed", settings={"endpoint": "https://x.invalid", "surprise": 1})
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "typed", settings={})
    row = registry.add(T1, TEXT, "typed", settings={"endpoint": "https://x.invalid"})
    assert row.settings == {"endpoint": "https://x.invalid", "region": "eu"}


def test_a_schema_cannot_smuggle_a_secret_in(registry, providers):
    class Schema(BaseModel):
        model_config = ConfigDict(extra="forbid")
        endpoint: str

    providers.register(TEXT, "typed", ConnectionMode.HOSTED, Hosted, settings_schema=Schema)
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "typed", settings={"endpoint": "Bearer abcdefghijklmnop"})


def test_manual_and_mock_take_no_settings(registry):
    with pytest.raises(InvalidConnection):
        registry.add(T1, Capability.TEXT, "mock", settings={"anything": 1})
    with pytest.raises(InvalidConnection):
        registry.add(T1, Capability.TEXT, "manual", scope=S.PIECE, scope_ref="p", settings={"anything": 1})
