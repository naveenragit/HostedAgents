"""Configuration contract tests for the research agent."""

from __future__ import annotations

import pytest

from research_agent.config import AgentConfig, ConfigError

_ENDPOINT = "https://acct.services.ai.azure.com/api/projects/proj"


def _set_required(env: pytest.MonkeyPatch) -> None:
    env.setenv("FOUNDRY_PROJECT_ENDPOINT", _ENDPOINT)
    env.setenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4-mini")


def test_reads_required_values(clean_env: pytest.MonkeyPatch) -> None:
    _set_required(clean_env)

    config = AgentConfig.from_env()

    assert config.project_endpoint == _ENDPOINT
    assert config.model_deployment_name == "gpt-5.4-mini"
    assert config.log_level == "INFO"
    assert config.max_input_chars == 8_000
    assert config.max_output_tokens == 1_200
    assert config.request_timeout_seconds == 60.0


def test_missing_endpoint_fails_fast(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4-mini")

    with pytest.raises(ConfigError, match="FOUNDRY_PROJECT_ENDPOINT"):
        AgentConfig.from_env()


def test_blank_value_is_treated_as_missing(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("FOUNDRY_PROJECT_ENDPOINT", "   ")
    clean_env.setenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4-mini")

    with pytest.raises(ConfigError):
        AgentConfig.from_env()


def test_model_name_falls_back_to_foundry_model_name(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("FOUNDRY_PROJECT_ENDPOINT", _ENDPOINT)
    clean_env.setenv("FOUNDRY_MODEL_NAME", "gpt-5.4-mini")

    assert AgentConfig.from_env().model_deployment_name == "gpt-5.4-mini"


def test_max_input_chars_is_capped_at_the_ceiling(clean_env: pytest.MonkeyPatch) -> None:
    _set_required(clean_env)
    clean_env.setenv("APP_MAX_INPUT_CHARS", "999999")

    assert AgentConfig.from_env().max_input_chars == 32_000


def test_output_and_timeout_limits_are_read_and_capped(clean_env: pytest.MonkeyPatch) -> None:
    _set_required(clean_env)
    clean_env.setenv("APP_MAX_OUTPUT_TOKENS", "999999")
    clean_env.setenv("APP_REQUEST_TIMEOUT_SECONDS", "999999")

    config = AgentConfig.from_env()

    assert config.max_output_tokens == 4_096
    assert config.request_timeout_seconds == 300.0


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_max_input_chars_is_rejected(clean_env: pytest.MonkeyPatch, value: str) -> None:
    _set_required(clean_env)
    clean_env.setenv("APP_MAX_INPUT_CHARS", value)

    with pytest.raises(ConfigError):
        AgentConfig.from_env()


@pytest.mark.parametrize("name", ["APP_MAX_OUTPUT_TOKENS", "APP_REQUEST_TIMEOUT_SECONDS"])
@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_output_or_timeout_value_is_rejected(
    clean_env: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    _set_required(clean_env)
    clean_env.setenv(name, value)

    with pytest.raises(ConfigError, match=name):
        AgentConfig.from_env()


def test_config_is_immutable(clean_env: pytest.MonkeyPatch) -> None:
    _set_required(clean_env)
    config = AgentConfig.from_env()

    with pytest.raises(AttributeError):
        config.model_deployment_name = "other"  # type: ignore[misc]
