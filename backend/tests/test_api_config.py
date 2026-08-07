"""Configuration contract tests for the local-only POC API."""

from __future__ import annotations

import pytest

from app.config import ConfigError, CredentialMode, get_settings

_FOUNDRY = "https://example-account.services.ai.azure.com/api/projects/example-project"
_MONGO = "mongodb+srv://cluster.mongocluster.cosmos.azure.com/?authMechanism=MONGODB-OIDC"

_ALL_VARS = (
    "FOUNDRY_PROJECT_ENDPOINT",
    "AGENT_NAMES",
    "FOUNDRY_CONVERSATION_AGENTS",
    "MONGO_CONNECTION_STRING",
    "MONGO_DATABASE",
    "API_POC_MODE",
    "API_POC_USER_ID",
    "AZURE_CREDENTIAL_MODE",
    "AZURE_MANAGED_IDENTITY_CLIENT_ID",
    "CORS_ALLOWED_ORIGINS",
    "API_MAX_INPUT_CHARS",
    "API_DELETION_TOMBSTONE_RETENTION_SECONDS",
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
    env.setenv("API_POC_MODE", "true")
    env.setenv("AZURE_CREDENTIAL_MODE", "local")


def test_reads_required_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)

    settings = get_settings()

    assert settings.foundry_project_endpoint == _FOUNDRY
    assert settings.agent_names == ("research-agent", "triage-agent")
    assert settings.foundry_conversation_agents == ("research-agent",)
    assert settings.mongo_connection_string == _MONGO
    assert settings.mongo_database == "agentchat"
    assert settings.poc_mode is True
    assert settings.poc_user_id == "poc-user"
    assert settings.credential_mode is CredentialMode.LOCAL
    assert settings.deletion_tombstone_retention_seconds == 86_400


def test_missing_connection_string_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", _FOUNDRY)
    monkeypatch.setenv("AGENT_NAMES", "research-agent")
    monkeypatch.setenv("API_POC_MODE", "true")
    monkeypatch.setenv("AZURE_CREDENTIAL_MODE", "local")

    with pytest.raises(ConfigError, match="MONGO_CONNECTION_STRING"):
        get_settings()


def test_foundry_conversation_agents_must_be_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("FOUNDRY_CONVERSATION_AGENTS", "unknown-agent")

    with pytest.raises(ConfigError, match="subset of AGENT_NAMES"):
        get_settings()


@pytest.mark.parametrize(
    "marker",
    ["CONTAINER_APP_NAME", "WEBSITE_INSTANCE_ID", "WEBSITE_SITE_NAME", "KUBERNETES_SERVICE_HOST"],
)
def test_poc_mode_is_refused_on_azure_compute(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv(marker, "something")

    with pytest.raises(ConfigError, match="API_POC_MODE is refused on Azure compute"):
        get_settings()


def test_poc_mode_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv("API_POC_MODE")

    with pytest.raises(ConfigError, match="API_POC_MODE=true"):
        get_settings()


def test_local_credential_is_refused_on_azure_compute(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("CONTAINER_APP_NAME", "api")

    with pytest.raises(ConfigError, match="API_POC_MODE is refused"):
        get_settings()


def test_poc_user_id_is_server_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("API_POC_USER_ID", "demo-session")

    assert get_settings().poc_user_id == "demo-session"


def test_blank_poc_user_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("API_POC_USER_ID", " ")

    with pytest.raises(ConfigError, match="API_POC_USER_ID"):
        get_settings()


def test_cors_origins_are_an_explicit_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")

    assert get_settings().cors_allowed_origins == (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )


@pytest.mark.parametrize(
    "origin",
    ["*", "https://app.example.com", "http://localhost:5173/path", "null"],
)
def test_cors_rejects_non_loopback_or_malformed_origins(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", origin)

    with pytest.raises(ConfigError, match="loopback HTTP origins"):
        get_settings()


def test_poc_mode_rejects_managed_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("AZURE_CREDENTIAL_MODE", "managed_identity")

    with pytest.raises(ConfigError, match="requires AZURE_CREDENTIAL_MODE=local"):
        get_settings()


def test_no_cors_origins_means_no_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)

    # An empty allow-list must never become "*".
    assert get_settings().cors_allowed_origins == ()


def test_max_input_chars_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("API_MAX_INPUT_CHARS", "999999")

    assert get_settings().max_input_chars == 32_000


def test_deletion_tombstone_retention_is_positive_and_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("API_DELETION_TOMBSTONE_RETENTION_SECONDS", "999999")

    assert get_settings().deletion_tombstone_retention_seconds == 604_800

    get_settings.cache_clear()
    monkeypatch.setenv("API_DELETION_TOMBSTONE_RETENTION_SECONDS", "0")
    with pytest.raises(ConfigError, match="must be greater than zero"):
        get_settings()


@pytest.mark.parametrize(
    ("connection_string", "message"),
    [
        (
            "mongodb://cluster.mongocluster.cosmos.azure.com/?authMechanism=MONGODB-OIDC",
            "mongodb\\+srv",
        ),
        (
            "mongodb+srv://user:secret@cluster.mongocluster.cosmos.azure.com/"
            "?authMechanism=MONGODB-OIDC",
            "embedded credentials",
        ),
        (
            "mongodb+srv://cluster.example.com/?authMechanism=MONGODB-OIDC",
            "Cosmos DB for MongoDB vCore",
        ),
        (
            "mongodb+srv://cluster.mongocluster.cosmos.azure.com/?tls=true",
            "authMechanism=MONGODB-OIDC",
        ),
    ],
)
def test_mongo_uri_is_strictly_validated(
    monkeypatch: pytest.MonkeyPatch,
    connection_string: str,
    message: str,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("MONGO_CONNECTION_STRING", connection_string)

    with pytest.raises(ConfigError, match=message):
        get_settings()
