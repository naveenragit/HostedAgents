"""Composition root for the research agent (container deployment).

Responsibilities, in order:
  1. Load local ``.env`` when present (no-op inside the Foundry sandbox).
  2. Read and validate configuration, failing fast on anything missing.
  3. Configure PII-safe logging.
  4. Build the responses app and run it.

No business logic lives here.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from research_agent.config import AgentConfig, ConfigError
from research_agent.handler import create_app
from research_agent.telemetry import configure_logging


def main() -> int:
    load_dotenv(override=False)

    try:
        config = AgentConfig.from_env()
    except ConfigError as exc:
        # stderr, not logging: this happens before logging is configured.
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    logger = configure_logging(config.log_level)
    logger.info(
        "starting research agent",
        extra={
            "model_deployment": config.model_deployment_name,
            "max_input_chars": config.max_input_chars,
        },
    )

    app = create_app(config)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
