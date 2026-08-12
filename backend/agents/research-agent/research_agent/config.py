"""Runtime configuration for the research agent.

Values are read once at start-up and validated eagerly so that a misconfigured
agent fails at container start rather than on the first user request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

# Absolute ceiling on user input length, independent of any env override.
# Bounds prompt-stuffing and runaway token cost.
_MAX_INPUT_CHARS_CEILING: Final[int] = 32_000
_DEFAULT_MAX_INPUT_CHARS: Final[int] = 8_000
_MAX_OUTPUT_TOKENS_CEILING: Final[int] = 4_096
_DEFAULT_MAX_OUTPUT_TOKENS: Final[int] = 1_200
_REQUEST_TIMEOUT_SECONDS_CEILING: Final[float] = 300.0
_DEFAULT_REQUEST_TIMEOUT_SECONDS: Final[float] = 60.0


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Immutable, validated runtime configuration."""

    project_endpoint: str
    model_deployment_name: str
    log_level: str
    max_input_chars: int
    max_output_tokens: int
    request_timeout_seconds: float
    model_gateway_url: str | None
    """APIM AI-gateway base URL. When set, model calls are routed through it.

    Routing through the gateway is what produces per-user token and cost
    telemetry. When unset the agent calls the Foundry project directly and no
    per-user attribution is recorded.
    """

    @classmethod
    def from_env(cls) -> AgentConfig:
        """Build configuration from environment variables.

        ``FOUNDRY_PROJECT_ENDPOINT`` is injected by the hosted agent platform at
        runtime. Locally it comes from ``.env`` (see ``.env.example``).
        """
        project_endpoint = _require("FOUNDRY_PROJECT_ENDPOINT")
        model_deployment_name = _require(
            "AZURE_AI_MODEL_DEPLOYMENT_NAME",
            fallback_name="FOUNDRY_MODEL_NAME",
        )

        return cls(
            project_endpoint=project_endpoint,
            model_deployment_name=model_deployment_name,
            log_level=os.environ.get("APP_LOG_LEVEL", "INFO").upper(),
            max_input_chars=_read_max_input_chars(),
            max_output_tokens=_read_positive_int(
                "APP_MAX_OUTPUT_TOKENS",
                default=_DEFAULT_MAX_OUTPUT_TOKENS,
                ceiling=_MAX_OUTPUT_TOKENS_CEILING,
            ),
            request_timeout_seconds=_read_positive_float(
                "APP_REQUEST_TIMEOUT_SECONDS",
                default=_DEFAULT_REQUEST_TIMEOUT_SECONDS,
                ceiling=_REQUEST_TIMEOUT_SECONDS_CEILING,
            ),
            model_gateway_url=_read_model_gateway_url(),
        )


def _require(name: str, *, fallback_name: str | None = None) -> str:
    """Return a non-blank environment variable, or raise ``ConfigError``."""
    value = os.environ.get(name, "").strip()
    if not value and fallback_name is not None:
        value = os.environ.get(fallback_name, "").strip()

    if not value:
        names = name if fallback_name is None else f"{name} (or {fallback_name})"
        raise ConfigError(
            f"Required environment variable {names} is not set. "
            "In Foundry this is injected by the platform; locally, copy "
            ".env.example to .env and fill it in."
        )
    return value


def _read_model_gateway_url() -> str | None:
    """Return a validated HTTPS gateway base URL, or ``None`` when not configured.

    Rejects anything that could smuggle credentials or redirect traffic: plaintext
    HTTP, embedded userinfo, query strings and fragments.
    """
    raw = os.environ.get("APP_MODEL_GATEWAY_URL", "").strip()
    if not raw:
        return None

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ConfigError("APP_MODEL_GATEWAY_URL is not a valid URL.") from exc

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            "APP_MODEL_GATEWAY_URL must be an https URL without credentials, "
            "query string or fragment."
        )

    return raw.rstrip("/")


def _read_max_input_chars() -> int:
    return _read_positive_int(
        "APP_MAX_INPUT_CHARS",
        default=_DEFAULT_MAX_INPUT_CHARS,
        ceiling=_MAX_INPUT_CHARS_CEILING,
    )


def _read_positive_int(name: str, *, default: int, ceiling: int) -> int:
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


def _read_positive_float(name: str, *, default: float, ceiling: float) -> float:
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
