"""Runtime configuration for the API service.

Parsed once at start-up and validated eagerly, so a misconfigured deployment fails
fast instead of surfacing as a 500 on the first request.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Final
from urllib.parse import parse_qs, urlsplit

_DEFAULT_MONGO_DATABASE: Final[str] = "agentchat"
_DEFAULT_CONVERSATIONS_COLLECTION: Final[str] = "conversations"
_DEFAULT_MESSAGES_COLLECTION: Final[str] = "messages"
_DEFAULT_TURNS_COLLECTION: Final[str] = "turns"
_DEFAULT_MAX_INPUT_CHARS: Final[int] = 8_000
_DEFAULT_MAX_OUTPUT_CHARS: Final[int] = 16_000
_DEFAULT_HISTORY_MAX_CHARS: Final[int] = 24_000
_DEFAULT_HISTORY_MAX_MESSAGES: Final[int] = 100
_DEFAULT_REQUEST_TIMEOUT_SECONDS: Final[float] = 120.0
_DEFAULT_TURN_LEASE_SECONDS: Final[int] = 180
_DEFAULT_DELETION_TOMBSTONE_RETENTION_SECONDS: Final[int] = 86_400
_DEFAULT_POC_USER_ID: Final[str] = "poc-user"
_MAX_INPUT_CHARS_CEILING: Final[int] = 32_000
_MAX_OUTPUT_CHARS_CEILING: Final[int] = 64_000
_MAX_HISTORY_CHARS_CEILING: Final[int] = 128_000
_MAX_HISTORY_MESSAGES_CEILING: Final[int] = 500
_MAX_DELETION_TOMBSTONE_RETENTION_SECONDS: Final[int] = 604_800
_MONGO_HOST_SUFFIX: Final[str] = ".mongocluster.cosmos.azure.com"

# Environment variables that only exist when running on Azure compute. Their
# presence is treated as proof that we are NOT on a developer workstation.
_AZURE_HOST_MARKERS: Final[tuple[str, ...]] = (
    "CONTAINER_APP_NAME",
    "WEBSITE_INSTANCE_ID",
    "WEBSITE_SITE_NAME",
    "KUBERNETES_SERVICE_HOST",
)


class CredentialMode(str, Enum):
    """Azure credential selected explicitly for the hosting environment."""

    LOCAL = "local"
    MANAGED_IDENTITY = "managed_identity"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, validated application settings."""

    foundry_project_endpoint: str
    agent_names: tuple[str, ...]
    foundry_conversation_agents: tuple[str, ...]
    mongo_connection_string: str
    mongo_database: str
    conversations_collection: str
    messages_collection: str
    turns_collection: str
    poc_mode: bool
    poc_user_id: str
    credential_mode: CredentialMode
    managed_identity_client_id: str | None
    cors_allowed_origins: tuple[str, ...]
    max_input_chars: int
    max_output_chars: int
    history_max_chars: int
    history_max_messages: int
    turn_lease_seconds: int
    deletion_tombstone_retention_seconds: int
    log_level: str
    request_timeout_seconds: float = field(default=_DEFAULT_REQUEST_TIMEOUT_SECONDS)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once."""
    poc_mode = _read_poc_mode()
    agent_names = _read_agent_names()

    mongo_connection_string = _require("MONGO_CONNECTION_STRING")
    _validate_mongo_connection_string(mongo_connection_string)
    request_timeout_seconds = _read_positive_float(
        "API_REQUEST_TIMEOUT_SECONDS", _DEFAULT_REQUEST_TIMEOUT_SECONDS, ceiling=600.0
    )
    turn_lease_seconds = _read_positive_int(
        "API_TURN_LEASE_SECONDS", _DEFAULT_TURN_LEASE_SECONDS, ceiling=3_600
    )
    if turn_lease_seconds <= request_timeout_seconds:
        raise ConfigError("API_TURN_LEASE_SECONDS must exceed API_REQUEST_TIMEOUT_SECONDS.")

    return Settings(
        foundry_project_endpoint=_require("FOUNDRY_PROJECT_ENDPOINT"),
        agent_names=agent_names,
        foundry_conversation_agents=_read_foundry_conversation_agents(agent_names),
        mongo_connection_string=mongo_connection_string,
        mongo_database=os.environ.get("MONGO_DATABASE", _DEFAULT_MONGO_DATABASE),
        conversations_collection=os.environ.get(
            "MONGO_CONVERSATIONS_COLLECTION", _DEFAULT_CONVERSATIONS_COLLECTION
        ),
        messages_collection=os.environ.get(
            "MONGO_MESSAGES_COLLECTION", _DEFAULT_MESSAGES_COLLECTION
        ),
        turns_collection=os.environ.get("MONGO_TURNS_COLLECTION", _DEFAULT_TURNS_COLLECTION),
        poc_mode=poc_mode,
        poc_user_id=_read_poc_user_id(),
        credential_mode=_read_credential_mode(),
        managed_identity_client_id=_optional("AZURE_MANAGED_IDENTITY_CLIENT_ID"),
        cors_allowed_origins=_read_cors_origins(),
        max_input_chars=_read_max_input_chars(),
        max_output_chars=_read_positive_int(
            "API_MAX_OUTPUT_CHARS",
            _DEFAULT_MAX_OUTPUT_CHARS,
            ceiling=_MAX_OUTPUT_CHARS_CEILING,
        ),
        history_max_chars=_read_positive_int(
            "API_HISTORY_MAX_CHARS",
            _DEFAULT_HISTORY_MAX_CHARS,
            ceiling=_MAX_HISTORY_CHARS_CEILING,
        ),
        history_max_messages=_read_positive_int(
            "API_HISTORY_MAX_MESSAGES",
            _DEFAULT_HISTORY_MAX_MESSAGES,
            ceiling=_MAX_HISTORY_MESSAGES_CEILING,
        ),
        turn_lease_seconds=turn_lease_seconds,
        deletion_tombstone_retention_seconds=_read_positive_int(
            "API_DELETION_TOMBSTONE_RETENTION_SECONDS",
            _DEFAULT_DELETION_TOMBSTONE_RETENTION_SECONDS,
            ceiling=_MAX_DELETION_TOMBSTONE_RETENTION_SECONDS,
        ),
        log_level=os.environ.get("API_LOG_LEVEL", "INFO").upper(),
        request_timeout_seconds=request_timeout_seconds,
    )


def _read_poc_mode() -> bool:
    """Require an explicit opt-in and refuse anonymous access on deployed compute."""
    if os.environ.get("API_POC_MODE", "").strip().lower() != "true":
        raise ConfigError("This sample requires the explicit opt-in API_POC_MODE=true.")

    markers = [name for name in _AZURE_HOST_MARKERS if os.environ.get(name)]
    if markers:
        raise ConfigError(
            "API_POC_MODE is refused on Azure compute "
            f"(detected {', '.join(markers)}). Add caller authentication before deployment."
        )
    return True


def _read_poc_user_id() -> str:
    value = os.environ.get("API_POC_USER_ID", _DEFAULT_POC_USER_ID).strip()
    if not value or len(value) > 128:
        raise ConfigError("API_POC_USER_ID must contain between 1 and 128 characters.")
    return value


def _read_credential_mode() -> CredentialMode:
    raw = _require("AZURE_CREDENTIAL_MODE").lower()
    try:
        mode = CredentialMode(raw)
    except ValueError as exc:
        valid = ", ".join(item.value for item in CredentialMode)
        raise ConfigError(f"AZURE_CREDENTIAL_MODE must be one of: {valid}. Got {raw!r}.") from exc

    if mode is not CredentialMode.LOCAL:
        raise ConfigError(
            "API_POC_MODE=true requires AZURE_CREDENTIAL_MODE=local. "
            "Add caller authentication before using managed identity."
        )
    return mode


def _read_agent_names() -> tuple[str, ...]:
    raw = _require("AGENT_NAMES")
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not names:
        raise ConfigError("AGENT_NAMES must list at least one agent name.")
    return names


def _read_foundry_conversation_agents(agent_names: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get("FOUNDRY_CONVERSATION_AGENTS", "research-agent")
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    unknown = sorted(set(names) - set(agent_names))
    if unknown:
        raise ConfigError(
            "FOUNDRY_CONVERSATION_AGENTS must be a subset of AGENT_NAMES. "
            f"Unknown: {', '.join(unknown)}."
        )
    return names


def _read_cors_origins() -> tuple[str, ...]:
    raw = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return ()
    origins = tuple(part.strip() for part in raw.split(",") if part.strip())
    for origin in origins:
        try:
            parsed = urlsplit(origin)
            hostname = parsed.hostname
            _ = parsed.port
            is_loopback = hostname == "localhost" or (
                hostname is not None and ipaddress.ip_address(hostname).is_loopback
            )
            valid = (
                parsed.scheme == "http"
                and is_loopback
                and parsed.username is None
                and parsed.password is None
                and parsed.path == ""
                and parsed.query == ""
                and parsed.fragment == ""
            )
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError("CORS_ALLOWED_ORIGINS may contain only loopback HTTP origins.")
    return origins


def _read_max_input_chars() -> int:
    return _read_positive_int(
        "API_MAX_INPUT_CHARS",
        _DEFAULT_MAX_INPUT_CHARS,
        ceiling=_MAX_INPUT_CHARS_CEILING,
    )


def _read_positive_int(name: str, default: int, *, ceiling: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc

    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero.")

    return min(value, ceiling)


def _read_positive_float(name: str, default: float, *, ceiling: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero.")
    return min(value, ceiling)


def _optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _validate_mongo_connection_string(connection_string: str) -> None:
    parsed = urlsplit(connection_string)
    if parsed.scheme.lower() != "mongodb+srv":
        raise ConfigError("MONGO_CONNECTION_STRING must use the mongodb+srv scheme.")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("MONGO_CONNECTION_STRING must not contain embedded credentials.")

    host = (parsed.hostname or "").lower()
    if not host.endswith(_MONGO_HOST_SUFFIX) or host == _MONGO_HOST_SUFFIX.removeprefix("."):
        raise ConfigError(
            "MONGO_CONNECTION_STRING must target an Azure Cosmos DB for MongoDB vCore host."
        )

    query = parse_qs(parsed.query)
    mechanisms = [value.upper() for value in query.get("authMechanism", [])]
    if mechanisms != ["MONGODB-OIDC"]:
        raise ConfigError(
            "MONGO_CONNECTION_STRING must set authMechanism=MONGODB-OIDC for keyless auth."
        )


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Required environment variable {name} is not set. "
            "Locally, copy .env.example to .env and fill it in."
        )
    return value
