"""Logging setup.

Deliberately PII-safe: we log sizes, identifiers and outcomes, never message
bodies. Azure SDK HTTP body/header logging is left disabled — enabling it would
write prompts and completions into Application Insights.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

_LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)-8s %(name)s %(message)s"


def configure_logging(level: str) -> logging.Logger:
    """Configure root logging and return the agent logger."""
    resolved = getattr(logging, level, logging.INFO)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    logging.basicConfig(
        level=resolved,
        format=_LOG_FORMAT,
        stream=sys.stdout,
        force=True,
    )

    # Azure SDK loggers are chatty and can echo request/response detail.
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("azure.identity").setLevel(logging.WARNING)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)

    return logging.getLogger("research_agent")
