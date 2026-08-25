"""Billing-sync meter mapping and usage normalization.

These rows become the authoritative money in the cost report, so a mapping or
unit-conversion mistake here silently misstates every per-user figure.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from billing_sync.meters import UNMAPPED_MODEL, resolve
from billing_sync.usage import BilledRow, aggregate, to_log_entries, tokens_for

_SINCE = datetime(2026, 8, 1, tzinfo=UTC)


def _record(
    *,
    meter: str,
    cost: float,
    quantity: float = 1.0,
    unit: str = "1M",
    day: int = 18,
    account: str = "contoso-foundry",
    price: float = 2.5,
    category: str = "Foundry Models",
) -> SimpleNamespace:
    return SimpleNamespace(
        meter_category=category,
        meter_name=meter,
        date=datetime(2026, 8, day, tzinfo=UTC),
        instance_name=(
            "/subscriptions/s/resourceGroups/rg/providers/"
            f"Microsoft.CognitiveServices/accounts/{account}"
        ),
        cost_in_usd=cost,
        quantity=quantity,
        unit_of_measure=unit,
        effective_price=price,
    )


@pytest.mark.parametrize(
    ("meter", "model", "direction", "tier"),
    [
        ("5.4 inp Gl 1M Tokens", "gpt-5.4", "input", "standard"),
        ("5.4 opt Gl 1M Tokens", "gpt-5.4", "output", "standard"),
        ("5.4 cd inp Gl 1M Tokens", "gpt-5.4", "cachedInput", "standard"),
        ("5.4 longco inp Gl 1M Tokens", "gpt-5.4", "input", "longContext"),
        ("5.4 longco cd inp Gl 1M Tokens", "gpt-5.4", "cachedInput", "longContext"),
        ("GPT 5.2 inp Gl 1M Tokens", "gpt-5.2", "input", "standard"),
        ("gpt-4o-0806-Inp-glbl Tokens", "gpt-4o", "input", "standard"),
        ("text-embedding-3-large-glbl Tokens", "text-embedding-3-large", "input", "standard"),
    ],
)
def test_maps_observed_billing_meters(meter: str, model: str, direction: str, tier: str) -> None:
    mapping = resolve(meter)

    assert mapping is not None
    assert (mapping.model, mapping.direction, mapping.tier) == (model, direction, tier)


def test_standard_and_long_context_meters_do_not_collide() -> None:
    # Long context prices the same deployment differently. If the standard
    # pattern also matched it, one model would get two conflicting input rates.
    standard = resolve("5.4 inp Gl 1M Tokens")
    long_context = resolve("5.4 longco inp Gl 1M Tokens")

    assert standard is not None and long_context is not None
    assert standard.tier == "standard"
    assert long_context.tier == "longContext"


def test_unknown_meter_is_unmapped_rather_than_guessed() -> None:
    assert resolve("Some Brand New Model inp Gl 1M Tokens") is None


@pytest.mark.parametrize(
    ("quantity", "unit", "expected"),
    [
        (0.000144, "1M", 144.0),
        (2.5, "1K", 2500.0),
        (1.0, "1M Tokens", 1000000.0),
    ],
)
def test_converts_pricing_units_to_a_common_token_basis(
    quantity: float, unit: str, expected: float
) -> None:
    assert tokens_for(quantity, unit) == pytest.approx(expected)


def test_unknown_pricing_unit_reports_zero_tokens_not_a_guess() -> None:
    # Cost is still ingested; inventing a token count would corrupt the
    # telemetry-vs-billing token comparison.
    assert tokens_for(5.0, "Units") == 0.0


def test_log_entry_matches_the_custom_table_schema() -> None:
    row = BilledRow(
        usage_date=datetime(2026, 8, 18, tzinfo=UTC),
        model_name="gpt-5.4",
        meter="5.4 inp Gl 1M Tokens",
        direction="input",
        rate_tier="standard",
        billed_cost_usd=5.844515,
        billed_tokens=2337806.0,
        effective_price_usd=2.5,
        unit_of_measure="1M",
        resource_name="contoso-foundry",
        subscription_id="sub-guid",
    )

    entries = to_log_entries([row])

    assert len(entries) == 1
    entry: dict[str, Any] = entries[0]
    # Column names are matched exactly at ingestion; a mismatch is dropped
    # silently rather than reported, so the schema is asserted in full.
    assert set(entry) == {
        "TimeGenerated",
        "UsageDate",
        "ModelName",
        "Meter",
        "Direction",
        "RateTier",
        "BilledCostUsd",
        "BilledTokens",
        "EffectivePriceUsd",
        "UnitOfMeasure",
        "ResourceName",
        "SubscriptionId",
    }
    assert entry["BilledCostUsd"] == pytest.approx(5.844515)
    assert entry["ModelName"] == "gpt-5.4"


def test_unmapped_constant_matches_what_the_workbook_filters_on() -> None:
    assert UNMAPPED_MODEL == "unmapped"


def test_aggregates_repeated_meter_days_into_one_row() -> None:
    records = [
        _record(meter="5.4 inp Gl 1M Tokens", cost=1.0, quantity=0.4),
        _record(meter="5.4 inp Gl 1M Tokens", cost=2.0, quantity=0.6),
    ]

    rows = aggregate(records, since=_SINCE, subscription_id="sub")

    assert len(rows) == 1
    assert rows[0].billed_cost_usd == pytest.approx(3.0)
    assert rows[0].billed_tokens == pytest.approx(1_000_000.0)


def test_keeps_unmapped_meters_so_billed_cost_stays_complete() -> None:
    # Dropping an unrecognised meter would shrink billed cost and make
    # reconciliation look healthier than it is.
    rows = aggregate(
        [_record(meter="Brand New Model inp Gl 1M Tokens", cost=9.0)],
        since=_SINCE,
        subscription_id="sub",
    )

    assert len(rows) == 1
    assert rows[0].model_name == UNMAPPED_MODEL
    assert rows[0].billed_cost_usd == pytest.approx(9.0)


def test_ignores_non_model_meters() -> None:
    rows = aggregate(
        [_record(meter="Standard LRS Data Stored", cost=4.0, category="Storage")],
        since=_SINCE,
        subscription_id="sub",
    )

    assert rows == []


def test_excludes_days_before_the_window() -> None:
    rows = aggregate(
        [_record(meter="5.4 inp Gl 1M Tokens", cost=1.0, day=18)],
        since=datetime(2026, 8, 19, tzinfo=UTC),
        subscription_id="sub",
    )

    assert rows == []


def test_account_filter_excludes_other_resources() -> None:
    records = [
        _record(meter="5.4 inp Gl 1M Tokens", cost=1.0, account="contoso-foundry"),
        _record(meter="5.4 inp Gl 1M Tokens", cost=8.0, account="some-other-account"),
    ]

    rows = aggregate(records, since=_SINCE, subscription_id="sub", account_name="contoso-foundry")

    assert len(rows) == 1
    assert rows[0].billed_cost_usd == pytest.approx(1.0)


def test_separates_long_context_from_standard_tier() -> None:
    records = [
        _record(meter="5.4 inp Gl 1M Tokens", cost=1.0),
        _record(meter="5.4 longco inp Gl 1M Tokens", cost=2.0),
    ]

    rows = aggregate(records, since=_SINCE, subscription_id="sub")

    tiers = {row.rate_tier for row in rows}
    assert tiers == {"standard", "longContext"}
    # Both belong to the same deployment and must both count toward its bill.
    assert {row.model_name for row in rows} == {"gpt-5.4"}


def test_reports_an_account_filter_that_matched_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A misspelled filter and "billing has not landed yet" both produce an empty
    # table. Without this the operator has no way to tell them apart.
    records = [_record(meter="5.4 inp Gl 1M Tokens", cost=9.0, account="real-account")]

    with caplog.at_level(logging.ERROR, logger="billing_sync.usage"):
        rows = aggregate(records, since=_SINCE, subscription_id="sub", account_name="typo-account")

    assert rows == []
    assert "typo-account" in caplog.text
    assert "real-account" in caplog.text


def test_stays_quiet_when_there_is_simply_no_billing_yet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="billing_sync.usage"):
        rows = aggregate([], since=_SINCE, subscription_id="sub", account_name="any-account")

    assert rows == []
    assert caplog.text == ""


def test_config_rejects_a_missing_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from billing_sync.config import ConfigError, SyncConfig

    monkeypatch.setenv("BILLING_SUBSCRIPTION_ID", "sub")
    monkeypatch.delenv("BILLING_DCE_ENDPOINT", raising=False)

    with pytest.raises(ConfigError, match="BILLING_DCE_ENDPOINT"):
        SyncConfig.from_env()


def test_config_defaults_the_lookback_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from billing_sync.config import SyncConfig

    for name, value in [
        ("BILLING_SUBSCRIPTION_ID", "sub"),
        ("BILLING_DCE_ENDPOINT", "https://dce.example.com"),
        ("BILLING_DCR_IMMUTABLE_ID", "dcr-123"),
    ]:
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("BILLING_LOOKBACK_DAYS", raising=False)
    monkeypatch.delenv("BILLING_ACCOUNT_NAME", raising=False)

    config = SyncConfig.from_env()

    # Billing restates recent days, so the window must be wider than one day.
    assert config.lookback_days > 1
    assert config.stream_name == "Custom-FoundryBilledCost_CL"


def test_config_rejects_an_out_of_range_lookback(monkeypatch: pytest.MonkeyPatch) -> None:
    from billing_sync.config import ConfigError, SyncConfig

    for name, value in [
        ("BILLING_SUBSCRIPTION_ID", "sub"),
        ("BILLING_DCE_ENDPOINT", "https://dce.example.com"),
        ("BILLING_DCR_IMMUTABLE_ID", "dcr-123"),
        ("BILLING_LOOKBACK_DAYS", "365"),
    ]:
        monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match="between 1 and 90"):
        SyncConfig.from_env()


def test_usage_records_without_a_date_are_skipped_not_defaulted() -> None:
    # A record with no date cannot be attributed to a billing day; defaulting it
    # to today would move real cost onto the wrong day.
    record = _record(meter="5.4 inp Gl 1M Tokens", cost=1.0)
    record.date = None

    assert aggregate([record], since=_SINCE, subscription_id="sub") == []
