"""Deterministic triage tools exposed to the model as function calls.

These are pure functions: no I/O, no network, no global state. That keeps them
cheap to unit test and makes the agent's classification auditable and repeatable
rather than a black-box model judgement.

Matching notes
--------------
Signals are regular expressions, not plain substrings. Real reports rarely use
canonical phrasing: "production is completely down" does not contain the literal
"production down", and "no users can log in" does not contain "login". Plain
substring checks silently under-classify both, which is the worst failure mode
for a severity tool. The patterns below tolerate filler words between the
significant terms and accept common spelling variants.
"""

from __future__ import annotations

import re
from typing import Final, Literal

Severity = Literal["sev1", "sev2", "sev3", "sev4"]

# Filler permitted between two significant terms (no sentence-crossing punctuation).
_GAP = r"[\w\s,'-]{0,30}"

_CANNOT = r"(?:can'?t|cannot|can not|unable to|no longer able to)"
_EVERYONE = r"(?:all|every|no|none of the)"
_SIGN_IN = r"(?:log\s?in|log\s?on|sign\s?in|authenticate)"

_SEV1_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p)
    for p in (
        r"\b(?:outage|data loss|data breach|security incident|ransomware)\b",
        # "production is completely down", "the service went offline"
        rf"\b(?:production|prod|site|service|system|platform|everything)\b{_GAP}"
        rf"\b(?:down|offline|unavailable|unreachable)\b",
        # "no users can log in", "all customers are unable to sign in"
        rf"\b{_EVERYONE}\b{_GAP}\b(?:users?|customers?|tenants?)\b{_GAP}\b{_CANNOT}\b",
        rf"\b{_CANNOT}\s+{_SIGN_IN}\b",
        rf"\b{_EVERYONE}\b{_GAP}\b(?:users?|customers?)\b{_GAP}\b(?:affected|impacted|blocked)\b",
        r"\b(?:complete|total|full)\s+(?:outage|failure|loss)\b",
    )
)

_SEV2_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p)
    for p in (
        r"\b(?:degraded|degradation|intermittent|flaky)\b",
        r"\b(?:timed?\s?out|timeouts?|timing out)\b",
        r"\b5\d{2}\b|\b5xx\b",
        r"\berror rate\b|\belevated errors?\b",
        r"\b(?:many|lots of|numerous|multiple|several)\b[\w\s,'-]{0,20}\b(?:users?|customers?)\b",
        r"\bpayments?\b[\w\s,'-]{0,20}\b(?:fail(?:ed|ing|s)?|declined?)\b",
        r"\b(?:corrupt|corrupted|corruption|data mismatch)\b",
    )
)

_SEV3_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p)
    for p in (
        r"\b(?:broken|breaks)\b",
        r"\bdoes\s+nothing\b",
        r"\b(?:does\s?not|doesn'?t|do\s?not|don'?t|not)\s+(?:work|working|respond|load|save)\b",
        r"\b(?:regression|regressed)\b",
        r"\b(?:crash(?:es|ing|ed)?|hangs?|freezes?)\b",
        r"\b(?:wrong|incorrect|unexpected)\s+(?:result|value|data|output|total)\b",
        r"\berrors?\b",
    )
)

_AREA_PATTERNS: Final[dict[str, tuple[str, ...]]] = {
    "identity": (
        _SIGN_IN,
        r"\bsso\b",
        r"\bmfa\b",
        r"\btokens?\b",
        r"\bauth(?:entication|orisation|orization)?\b",
        r"\bpasswords?\b",
        r"\bcredentials?\b",
    ),
    "billing": (
        r"\binvoices?\b",
        r"\bpayments?\b",
        r"\bcharges?\b",
        r"\bsubscriptions?\b",
        r"\brefunds?\b",
        r"\bbilling\b",
        r"\bcheckout\b",
    ),
    "data-platform": (
        r"\bqueries\b|\bquery\b",
        r"\bexports?\b|\bexporting\b",
        r"\breports?\b",
        r"\bdatasets?\b",
        r"\bpipelines?\b",
        r"\betl\b",
    ),
    "web-client": (
        r"\bbuttons?\b",
        r"\bpages?\b",
        r"\bbrowser\b",
        r"\bsafari\b|\bchrome\b|\bfirefox\b|\bedge\b",
        r"\bui\b|\bfront-?end\b",
        r"\bcss\b|\blayout\b",
    ),
    "api": (
        r"\bendpoints?\b",
        r"\bapis?\b",
        r"\brest\b|\bgraphql\b",
        r"\bwebhooks?\b",
        r"\bstatus code\b",
        r"\brate limit\b",
    ),
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

_COMPILED_AREA_PATTERNS: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    area: tuple(re.compile(p) for p in patterns) for area, patterns in _AREA_PATTERNS.items()
}


def classify_severity(report: str) -> Severity:
    """Classify a customer issue report into a severity band.

    Args:
        report: The raw customer issue description.

    Returns:
        One of ``sev1`` (critical), ``sev2`` (major), ``sev3`` (minor) or
        ``sev4`` (cosmetic / informational).
    """
    text = _normalise(report)

    if _matches_any(text, _SEV1_PATTERNS):
        return "sev1"
    if _matches_any(text, _SEV2_PATTERNS):
        return "sev2"
    if _matches_any(text, _SEV3_PATTERNS):
        return "sev3"
    return "sev4"


def identify_area(report: str) -> str:
    """Identify the product area a customer issue report belongs to.

    Args:
        report: The raw customer issue description.

    Returns:
        A product area slug, or ``unknown`` when nothing matches. When several
        areas match, the one with the most distinct hits wins.
    """
    text = _normalise(report)

    best_area = "unknown"
    best_hits = 0
    for area, patterns in _COMPILED_AREA_PATTERNS.items():
        hits = sum(1 for pattern in patterns if pattern.search(text))
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


def _normalise(text: str) -> str:
    """Lower-case and collapse whitespace so patterns see a predictable shape."""
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)
