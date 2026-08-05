"""Deterministic triage tools exposed to the model as function calls.

These are pure functions: no I/O, no network, no global state. That keeps them
cheap to unit test and makes the agent's classification auditable and repeatable
rather than a black-box model judgement.
"""

from __future__ import annotations

from typing import Final, Literal

Severity = Literal["sev1", "sev2", "sev3", "sev4"]

_SEV1_SIGNALS: Final[frozenset[str]] = frozenset(
    {
        "outage",
        "data loss",
        "data breach",
        "security incident",
        "all users",
        "cannot log in",
        "production down",
    }
)

_SEV2_SIGNALS: Final[frozenset[str]] = frozenset(
    {
        "degraded",
        "timeout",
        "5xx",
        "error rate",
        "many users",
        "payment failed",
        "corrupt",
    }
)

_SEV3_SIGNALS: Final[frozenset[str]] = frozenset(
    {"broken", "does not work", "doesn't work", "regression", "crash", "wrong result"}
)

_AREA_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "identity": ("login", "sign in", "sso", "mfa", "token", "auth", "password"),
    "billing": ("invoice", "payment", "charge", "subscription", "refund", "billing"),
    "data-platform": ("query", "export", "report", "dataset", "pipeline", "etl"),
    "web-client": ("button", "page", "browser", "safari", "chrome", "ui", "css"),
    "api": ("endpoint", "api", "rest", "webhook", "status code", "rate limit"),
}

_TEAM_BY_AREA: Final[dict[str, str]] = {
    "identity": "identity-oncall",
    "billing": "commerce-oncall",
    "data-platform": "data-oncall",
    "web-client": "frontend-oncall",
    "api": "platform-oncall",
    "unknown": "triage-rotation",
}

_ESCALATION_TEAM: Final[str] = "incident-commander"


def classify_severity(report: str) -> Severity:
    """Classify a customer issue report into a severity band.

    Args:
        report: The raw customer issue description.

    Returns:
        One of ``sev1`` (critical), ``sev2`` (major), ``sev3`` (minor) or
        ``sev4`` (cosmetic / informational).
    """
    text = report.casefold()

    if _contains_any(text, _SEV1_SIGNALS):
        return "sev1"
    if _contains_any(text, _SEV2_SIGNALS):
        return "sev2"
    if _contains_any(text, _SEV3_SIGNALS):
        return "sev3"
    return "sev4"


def identify_area(report: str) -> str:
    """Identify the product area a customer issue report belongs to.

    Args:
        report: The raw customer issue description.

    Returns:
        A product area slug, or ``unknown`` when no area matches.
    """
    text = report.casefold()

    best_area = "unknown"
    best_hits = 0
    for area, keywords in _AREA_KEYWORDS.items():
        hits = sum(1 for keyword in keywords if keyword in text)
        if hits > best_hits:
            best_area, best_hits = area, hits

    return best_area


def route_to_team(area: str, severity: Severity) -> str:
    """Return the on-call team that owns an issue.

    Args:
        area: A product area slug, as returned by ``identify_area``.
        severity: The severity band, as returned by ``classify_severity``.

    Returns:
        The name of the owning on-call rotation. ``sev1`` always escalates to
        the incident commander regardless of area.
    """
    if severity == "sev1":
        return _ESCALATION_TEAM
    return _TEAM_BY_AREA.get(area, _TEAM_BY_AREA["unknown"])


def _contains_any(text: str, signals: frozenset[str]) -> bool:
    return any(signal in text for signal in signals)
