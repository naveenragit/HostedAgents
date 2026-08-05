"""Runtime configuration for the research agent.

Values are read once at start-up and validated eagerly so that a misconfigured
agent fails at container start rather than on the first user request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

# Absolute ceiling on user input length, independent of any env override.
# Bounds prompt-stuffing and runaway token cost.
_MAX_INPUT_CHARS_CEILING: Final[int] = 32_000
_DEFAULT_MAX_INPUT_CHARS: Final[int] = 8_000


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Immutable, validated runtime configuration."""

    project_endpoint: str
    model_deployment_name: str
    log_level: str
    max_input_chars: int

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


def _read_max_input_chars() -> int:
    raw = os.environ.get("APP_MAX_INPUT_CHARS", "").strip()
    if not raw:
        return _DEFAULT_MAX_INPUT_CHARS

    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"APP_MAX_INPUT_CHARS must be an integer, got {raw!r}.") from exc

    if value <= 0:
        raise ConfigError("APP_MAX_INPUT_CHARS must be greater than zero.")

    return min(value, _MAX_INPUT_CHARS_CEILING)
