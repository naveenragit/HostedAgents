"""Behaviour tests for the deterministic triage tools.

The `test_real_reports_*` cases are wordings that were actually sent through the
deployed agent and misclassified by the original substring matcher. They are kept
as regression tests: they are the reason the implementation uses regexes.
"""

from __future__ import annotations

import pytest

from triage_agent.tools import classify_severity, identify_area, route_to_team

# --------------------------------------------------------------------------- #
# Regressions from live traffic
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("report", "severity", "area"),
    [
        # Was sev4/unknown: "production down" is not a substring of "production
        # is completely down", and "login" is not a substring of "log in".
        (
            "Production is completely down, no users can log in at all since the last deploy.",
            "sev1",
            "identity",
        ),
        # Was sev4: "does nothing" was not in the sev3 signal list at all.
        (
            "The export button does nothing in Safari for several enterprise customers.",
            "sev2",  # "several ... customers" is a sev2 signal
            "web-client",
        ),
    ],
)
def test_real_reports_are_classified_sensibly(report: str, severity: str, area: str) -> None:
    assert classify_severity(report) == severity
    assert identify_area(report) == area


# --------------------------------------------------------------------------- #
# Severity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("Total production outage, no one can reach the service", "sev1"),
        ("Suspected data breach affecting customer records", "sev1"),
        ("The whole platform is offline", "sev1"),
        ("All customers are unable to sign in", "sev1"),
        ("Users cannot log in", "sev1"),
        ("Error rate is elevated and requests time out intermittently", "sev2"),
        ("We are seeing 503 responses from the gateway", "sev2"),
        ("Payments are failing at checkout", "sev2"),
        ("The export button is broken on the reports page", "sev3"),
        ("The dashboard does not load", "sev3"),
        ("The app crashes when I open settings", "sev3"),
        ("The footer copyright year is slightly misaligned", "sev4"),
        ("Could you add a dark mode option?", "sev4"),
    ],
)
def test_classify_severity(report: str, expected: str) -> None:
    assert classify_severity(report) == expected


def test_classify_severity_is_case_and_whitespace_insensitive() -> None:
    assert classify_severity("PRODUCTION   IS\n\nDOWN") == "sev1"


def test_severity_precedence_prefers_the_most_severe_signal() -> None:
    # Contains a sev3 signal ("broken") and a sev1 signal ("outage").
    assert classify_severity("The login page is broken — full outage for all users") == "sev1"


# --------------------------------------------------------------------------- #
# Area
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("Users cannot complete MFA during sign in", "identity"),
        ("Password reset emails never arrive", "identity"),
        ("The invoice shows a duplicate charge on the subscription", "billing"),
        ("Our nightly ETL pipeline fails to export the dataset", "data-platform"),
        ("The submit button does nothing in Safari", "web-client"),
        ("The webhook endpoint returns a rate limit status code", "api"),
        ("Something feels a bit off lately", "unknown"),
    ],
)
def test_identify_area(report: str, expected: str) -> None:
    assert identify_area(report) == expected


def test_area_with_most_distinct_hits_wins() -> None:
    # Mentions one billing term but three identity terms.
    report = "After the invoice run, SSO tokens expire and users cannot authenticate"
    assert identify_area(report) == "identity"


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def test_sev1_always_escalates_regardless_of_area() -> None:
    assert route_to_team("web-client", "sev1") == "incident-commander"
    assert route_to_team("unknown", "sev1") == "incident-commander"


@pytest.mark.parametrize(
    ("area", "expected"),
    [
        ("identity", "identity-oncall"),
        ("billing", "commerce-oncall"),
        ("data-platform", "data-oncall"),
        ("web-client", "frontend-oncall"),
        ("api", "platform-oncall"),
    ],
)
def test_non_critical_issues_route_to_the_owning_team(area: str, expected: str) -> None:
    assert route_to_team(area, "sev3") == expected


def test_unrecognised_area_falls_back_to_the_triage_rotation() -> None:
    assert route_to_team("not-a-real-area", "sev2") == "triage-rotation"


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #


def test_tools_are_pure_and_repeatable() -> None:
    report = "Payment failed for many users during checkout"
    assert classify_severity(report) == classify_severity(report)
    assert identify_area(report) == identify_area(report)


def test_empty_report_is_handled() -> None:
    assert classify_severity("") == "sev4"
    assert identify_area("") == "unknown"


def test_untrusted_input_cannot_break_matching() -> None:
    # Regex metacharacters in user input must not raise or alter behaviour.
    hostile = "(((*+?[unclosed  \\ ] production is down"
    assert classify_severity(hostile) == "sev1"
