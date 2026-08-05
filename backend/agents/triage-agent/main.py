"""Composition root for the triage agent (code deployment).

Deployed via source ZIP upload: ``azd deploy triage-agent`` zips this directory
(honouring ``.agentignore``) and Foundry installs ``requirements.txt`` and builds
the runtime image server-side on ``python_3_13``.

``ResponsesHostServer`` owns the HTTP surface — port 8088, ``POST /responses``
and the ``GET /readiness`` health probe.
"""

from __future__ import annotations

import logging
import sys

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from triage_agent.config import AgentConfig, ConfigError
from triage_agent.instructions import SYSTEM_INSTRUCTIONS
from triage_agent.tools import classify_severity, identify_area, route_to_team

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"


def _configure_logging(level: str) -> logging.Logger:
    """Configure PII-safe logging: shapes and outcomes only, never message bodies."""
    resolved = getattr(logging, level, logging.INFO)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    logging.basicConfig(level=resolved, format=_LOG_FORMAT, stream=sys.stdout, force=True)
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("azure.identity").setLevel(logging.WARNING)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    return logging.getLogger("triage_agent")


def build_agent(config: AgentConfig) -> Agent:
    """Construct the Agent Framework agent from validated configuration."""
    # Keyless auth: the platform-provisioned per-agent Entra identity in Foundry,
    # the signed-in developer locally. No API keys anywhere.
    client = FoundryChatClient(
        project_endpoint=config.project_endpoint,
        model=config.model_deployment_name,
        credential=DefaultAzureCredential(),
    )

    return Agent(
        client=client,
        instructions=SYSTEM_INSTRUCTIONS,
        tools=[classify_severity, identify_area, route_to_team],
        # History is managed by the hosting platform, so the agent stores no
        # conversation content of its own.
        default_options={"store": False},
    )


def main() -> int:
    load_dotenv(override=False)

    try:
        config = AgentConfig.from_env()
    except ConfigError as exc:
        # stderr, not logging: this happens before logging is configured.
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    logger = _configure_logging(config.log_level)
    logger.info(
        "starting triage agent",
        extra={
            "model_deployment": config.model_deployment_name,
            "max_input_chars": config.max_input_chars,
        },
    )

    ResponsesHostServer(build_agent(config)).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
