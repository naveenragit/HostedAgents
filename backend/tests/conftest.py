"""Shared test fixtures.

Tests exercise only the pure, dependency-free layers (config parsing and triage
tools). Neither agent's SDK packages are installed in the root dev venv, so the
suite runs fast and offline with no Azure calls.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

_AGENT_ENV_VARS = (
    "FOUNDRY_PROJECT_ENDPOINT",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME",
    "FOUNDRY_MODEL_NAME",
    "APP_LOG_LEVEL",
    "APP_MAX_INPUT_CHARS",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Remove all agent environment variables so each test starts from zero."""
    for name in _AGENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield monkeypatch
