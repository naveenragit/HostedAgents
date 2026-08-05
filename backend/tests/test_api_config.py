"""Configuration contract tests for the API service.

The dev-auth guard is the important one: `AUTH_MODE=dev` trusts a client-supplied
header, so it must be impossible to enable on Azure compute.
"""

from __future__ import annotations

import pytest

from app.config import AuthMode, ConfigError, get_settings

_FOUNDRY = "https://foundry-pocs.services.ai.azure.com/api/projects/foundry-proj-pocs"
_MONGO = "mongodb+srv://cluster.mongocluster.cosmos.azure.com/?authMechanism=MONGODB-OIDC"

_ALL_VARS = (
    "FOUNDRY_PROJECT_ENDPOINT",
    "AGENT_NAMES",
    "MONGO_CONNECTION_STRING",
    "MONGO_DATABASE",
    "AUTH_MODE",
    "AZURE_TENANT_ID",
    "API_AUDIENCE",
    "CORS_ALLOWED_ORIGINS",
    "API_MAX_INPUT_CHARS",
    "CONTAINER_APP_NAME",
    "WEBSITE_INSTANCE_ID",
    "WEBSITE_SITE_NAME",
    "KUBERNETES_SERVICE_HOST",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every variable the settings reader looks at, and the lru_cache."""
    for name in _ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def _set_required(env: pytest.MonkeyPatch) -> None:
    env.setenv("FOUNDRY_PROJECT_ENDPOINT", _FOUNDRY)
    env.setenv("AGENT_NAMES", "research-agent,triage-agent")
    env.setenv("MONGO_CONNECTION_STRING", _MONGO)
    env.setenv("AUTH_MODE", "dev")


def test_reads_required_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)

    settings = get_settings()

    assert settings.foundry_project_endpoint == _FOUNDRY
    assert settings.agent_names == ("research-agent", "triage-agent")
    assert settings.mongo_connection_string == _MONGO
    assert settings.mongo_database == "agentchat"
    assert settings.auth_mode is AuthMode.DEV


def test_missing_connection_string_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", _FOUNDRY)
    monkeypatch.setenv("AGENT_NAMES", "research-agent")
    monkeypatch.setenv("AUTH_MODE", "dev")

    with pytest.raises(ConfigError, match="MONGO_CONNECTION_STRING"):
        get_settings()


@pytest.mark.parametrize(
    "marker",
    ["CONTAINER_APP_NAME", "WEBSITE_INSTANCE_ID", "WEBSITE_SITE_NAME", "KUBERNETES_SERVICE_HOST"],
)
def test_dev_auth_is_refused_on_azure_compute(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv(marker, "something")

    with pytest.raises(ConfigError, match="refused on Azure compute"):
        get_settings()


def test_entra_mode_requires_tenant_and_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("AUTH_MODE", "entra")

    with pytest.raises(ConfigError, match="AZURE_TENANT_ID"):
        get_settings()


def test_entra_mode_builds_issuer_and_jwks_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("AUTH_MODE", "entra")
    monkeypatch.setenv("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("API_AUDIENCE", "api://hosted-agents")

    entra = get_settings().entra

    assert entra is not None
    assert entra.issuer.endswith("/v2.0")
    assert "discovery/v2.0/keys" in entra.jwks_uri


def test_unknown_auth_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("AUTH_MODE", "anonymous")

    with pytest.raises(ConfigError, match="AUTH_MODE must be one of"):
        get_settings()


def test_cors_origins_are_an_explicit_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173, https://app.example.com")

    assert get_settings().cors_allowed_origins == (
        "http://localhost:5173",
        "https://app.example.com",
    )


def test_no_cors_origins_means_no_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)

    # An empty allow-list must never become "*".
    assert get_settings().cors_allowed_origins == ()


def test_max_input_chars_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("API_MAX_INPUT_CHARS", "999999")

    assert get_settings().max_input_chars == 32_000
