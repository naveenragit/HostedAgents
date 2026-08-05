"""Runtime configuration for the API service.

Parsed once at start-up and validated eagerly, so a misconfigured deployment fails
fast instead of surfacing as a 500 on the first request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Final

_DEFAULT_MONGO_DATABASE: Final[str] = "agentchat"
_DEFAULT_CONVERSATIONS_COLLECTION: Final[str] = "conversations"
_DEFAULT_MESSAGES_COLLECTION: Final[str] = "messages"
_DEFAULT_MAX_INPUT_CHARS: Final[int] = 8_000
_MAX_INPUT_CHARS_CEILING: Final[int] = 32_000

# Environment variables that only exist when running on Azure compute. Their
# presence is treated as proof that we are NOT on a developer workstation.
_AZURE_HOST_MARKERS: Final[tuple[str, ...]] = (
    "CONTAINER_APP_NAME",
    "WEBSITE_INSTANCE_ID",
    "WEBSITE_SITE_NAME",
    "KUBERNETES_SERVICE_HOST",
)


class AuthMode(str, Enum):
    """How the caller's identity is established."""

    ENTRA = "entra"
    """Validate a Microsoft Entra ID JWT and derive the user from the `oid` claim."""

    DEV = "dev"
    """Local development only. Trusts a header. Refused on Azure compute."""


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class EntraConfig:
    """Microsoft Entra ID token-validation settings."""

    tenant_id: str
    api_audience: str

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    @property
    def jwks_uri(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, validated application settings."""

    foundry_project_endpoint: str
    agent_names: tuple[str, ...]
    mongo_connection_string: str
    mongo_database: str
    conversations_collection: str
    messages_collection: str
    auth_mode: AuthMode
    entra: EntraConfig | None
    cors_allowed_origins: tuple[str, ...]
    max_input_chars: int
    log_level: str
    request_timeout_seconds: float = field(default=120.0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once."""
    auth_mode = _read_auth_mode()

    return Settings(
        foundry_project_endpoint=_require("FOUNDRY_PROJECT_ENDPOINT"),
        agent_names=_read_agent_names(),
        mongo_connection_string=_require("MONGO_CONNECTION_STRING"),
        mongo_database=os.environ.get("MONGO_DATABASE", _DEFAULT_MONGO_DATABASE),
        conversations_collection=os.environ.get(
            "MONGO_CONVERSATIONS_COLLECTION", _DEFAULT_CONVERSATIONS_COLLECTION
        ),
        messages_collection=os.environ.get(
            "MONGO_MESSAGES_COLLECTION", _DEFAULT_MESSAGES_COLLECTION
        ),
        auth_mode=auth_mode,
        entra=_read_entra_config() if auth_mode is AuthMode.ENTRA else None,
        cors_allowed_origins=_read_cors_origins(),
        max_input_chars=_read_max_input_chars(),
        log_level=os.environ.get("API_LOG_LEVEL", "INFO").upper(),
    )


def _read_auth_mode() -> AuthMode:
    raw = os.environ.get("AUTH_MODE", AuthMode.ENTRA.value).strip().lower()

    try:
        mode = AuthMode(raw)
    except ValueError as exc:
        valid = ", ".join(m.value for m in AuthMode)
        raise ConfigError(f"AUTH_MODE must be one of: {valid}. Got {raw!r}.") from exc

    if mode is AuthMode.DEV:
        # Fail closed. Dev auth trusts a client-supplied header, so it must never
        # be reachable from a deployed environment.
        markers = [name for name in _AZURE_HOST_MARKERS if os.environ.get(name)]
        if markers:
            raise ConfigError(
                "AUTH_MODE=dev is refused on Azure compute "
                f"(detected {', '.join(markers)}). Use AUTH_MODE=entra."
            )

    return mode


def _read_entra_config() -> EntraConfig:
    return EntraConfig(
        tenant_id=_require("AZURE_TENANT_ID"),
        api_audience=_require("API_AUDIENCE"),
    )


def _read_agent_names() -> tuple[str, ...]:
    raw = _require("AGENT_NAMES")
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not names:
        raise ConfigError("AGENT_NAMES must list at least one agent name.")
    return names


def _read_cors_origins() -> tuple[str, ...]:
    raw = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return ()
    # Explicit allow-list only. A wildcard is never derived automatically.
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _read_max_input_chars() -> int:
    raw = os.environ.get("API_MAX_INPUT_CHARS", "").strip()
    if not raw:
        return _DEFAULT_MAX_INPUT_CHARS

    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"API_MAX_INPUT_CHARS must be an integer, got {raw!r}.") from exc

    if value <= 0:
        raise ConfigError("API_MAX_INPUT_CHARS must be greater than zero.")

    return min(value, _MAX_INPUT_CHARS_CEILING)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Required environment variable {name} is not set. "
            "Locally, copy .env.example to .env and fill it in."
        )
    return value
