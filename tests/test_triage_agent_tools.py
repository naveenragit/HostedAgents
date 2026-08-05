"""Behaviour tests for the deterministic triage tools."""

from __future__ import annotations

import pytest

from triage_agent.tools import classify_severity, identify_area, route_to_team


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("Total production outage, no one can reach the service", "sev1"),
        ("Suspected data breach affecting customer records", "sev1"),
        ("Error rate is elevated and requests time out intermittently", "sev2"),
        ("The export button is broken on the reports page", "sev3"),
        ("The footer copyright year is slightly misaligned", "sev4"),
    ],
)
def test_classify_severity(report: str, expected: str) -> None:
    assert classify_severity(report) == expected


def test_classify_severity_is_case_insensitive() -> None:
    assert classify_severity("PRODUCTION DOWN") == "sev1"


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("Users cannot complete MFA during sign in", "identity"),
        ("The invoice shows a duplicate charge on the subscription", "billing"),
        ("Our nightly ETL pipeline fails to export the dataset", "data-platform"),
        ("The submit button does nothing in Safari", "web-client"),
        ("The webhook endpoint returns a rate limit status code", "api"),
        ("Something feels a bit off lately", "unknown"),
    ],
)
def test_identify_area(report: str, expected: str) -> None:
    assert identify_area(report) == expected


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


def test_tools_are_pure_and_repeatable() -> None:
    report = "Payment failed for many users during checkout"
    assert classify_severity(report) == classify_severity(report)
    assert identify_area(report) == identify_area(report)
