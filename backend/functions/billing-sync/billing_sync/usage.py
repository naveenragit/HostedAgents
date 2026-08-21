"""Normalization of Cost Management usage records into billed rows.

Deliberately free of Azure SDK imports so the suite stays offline, matching the
rest of the repository. Records are read through ``getattr``, so this works with
SDK models and with plain objects in tests. The SDK call itself lives in
``billing_sync.costmanagement``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final

from billing_sync.meters import UNMAPPED_MODEL, resolve

_LOGGER: Final = logging.getLogger("billing_sync.usage")

MODEL_METER_CATEGORY: Final[str] = "Foundry Models"

# Pricing units seen on model meters. Anything else cannot be converted to a
# token count, so the row still carries its cost but reports zero tokens rather
# than a fabricated number.
_UNIT_TO_TOKENS: Final[dict[str, float]] = {"1M": 1_000_000.0, "1K": 1_000.0}


@dataclass(frozen=True)
class BilledRow:
    usage_date: datetime
    model_name: str
    meter: str
    direction: str
    rate_tier: str
    billed_cost_usd: float
    billed_tokens: float
    effective_price_usd: float
    unit_of_measure: str
    resource_name: str
    subscription_id: str

    def to_log_entry(self, generated_at: datetime) -> dict[str, Any]:
        return {
            "TimeGenerated": generated_at.isoformat(),
            "UsageDate": self.usage_date.isoformat(),
            "ModelName": self.model_name,
            "Meter": self.meter,
            "Direction": self.direction,
            "RateTier": self.rate_tier,
            "BilledCostUsd": self.billed_cost_usd,
            "BilledTokens": self.billed_tokens,
            "EffectivePriceUsd": self.effective_price_usd,
            "UnitOfMeasure": self.unit_of_measure,
            "ResourceName": self.resource_name,
            "SubscriptionId": self.subscription_id,
        }


def tokens_for(quantity: float, unit_of_measure: str) -> float:
    """Convert a metered quantity to tokens, or 0.0 when the unit is unknown."""
    for prefix, multiplier in _UNIT_TO_TOKENS.items():
        if unit_of_measure.startswith(prefix):
            return quantity * multiplier
    return 0.0


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _as_str(value: object) -> str:
    return str(value) if value else ""


def aggregate(
    records: Iterable[Any],
    *,
    since: datetime,
    subscription_id: str,
    account_name: str | None = None,
) -> list[BilledRow]:
    """Aggregate raw usage records into one row per meter, day and resource."""
    rows: dict[tuple[str, str, str], BilledRow] = {}
    scanned = 0
    unmapped: set[str] = set()

    for record in records:
        scanned += 1
        if getattr(record, "meter_category", None) != MODEL_METER_CATEGORY:
            continue

        usage_date = getattr(record, "date", None)
        if not isinstance(usage_date, datetime):
            # Without a date the charge cannot be placed on a billing day.
            # Defaulting it would move real cost onto the wrong day.
            continue
        if usage_date.tzinfo is None:
            usage_date = usage_date.replace(tzinfo=UTC)
        if usage_date < since:
            continue

        resource_name = _as_str(getattr(record, "instance_name", "")).rsplit("/", 1)[-1]
        if account_name and resource_name != account_name:
            continue

        meter = _as_str(getattr(record, "meter_name", ""))
        mapping = resolve(meter)
        if mapping is None:
            unmapped.add(meter)

        unit = _as_str(getattr(record, "unit_of_measure", ""))
        cost = _as_float(getattr(record, "cost_in_usd", None))
        tokens = tokens_for(_as_float(getattr(record, "quantity", None)), unit)
        price = _as_float(getattr(record, "effective_price", None))

        key = (usage_date.date().isoformat(), meter, resource_name)
        existing = rows.get(key)
        if existing is None:
            rows[key] = BilledRow(
                usage_date=usage_date,
                model_name=mapping.model if mapping else UNMAPPED_MODEL,
                meter=meter,
                direction=mapping.direction if mapping else "",
                rate_tier=mapping.tier if mapping else "",
                billed_cost_usd=cost,
                billed_tokens=tokens,
                effective_price_usd=price,
                unit_of_measure=unit,
                resource_name=resource_name,
                subscription_id=subscription_id,
            )
        else:
            rows[key] = replace(
                existing,
                billed_cost_usd=existing.billed_cost_usd + cost,
                billed_tokens=existing.billed_tokens + tokens,
                # Price is per unit, so it is carried rather than summed.
                effective_price_usd=price or existing.effective_price_usd,
                unit_of_measure=existing.unit_of_measure or unit,
            )

    if unmapped:
        # Not an error: unmapped meters are still ingested so billed cost stays
        # complete. They surface in the workbook as ModelName 'unmapped'.
        _LOGGER.warning(
            "%d unmapped meter(s); add them to model-meter-map.json: %s",
            len(unmapped),
            ", ".join(sorted(unmapped)),
        )

    ordered = sorted(rows.values(), key=lambda r: (r.usage_date, r.model_name, r.meter))
    _LOGGER.info("scanned %d usage record(s), produced %d billed row(s)", scanned, len(ordered))
    return ordered


def to_log_entries(rows: Sequence[BilledRow]) -> list[dict[str, Any]]:
    generated_at = datetime.now(UTC)
    return [row.to_log_entry(generated_at) for row in rows]
