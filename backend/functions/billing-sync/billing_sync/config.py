"""Validated configuration for the billing-sync function."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

# Billing restates recent days as charges settle, so each run re-reads a window
# and the custom table is queried with de-duplication rather than assuming one
# row per meter per day.
_DEFAULT_LOOKBACK_DAYS: Final[int] = 14


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


@dataclass(frozen=True)
class SyncConfig:
    subscription_id: str
    dce_endpoint: str
    dcr_immutable_id: str
    stream_name: str
    lookback_days: int
    account_name: str | None

    @classmethod
    def from_env(cls) -> SyncConfig:
        subscription_id = _required("BILLING_SUBSCRIPTION_ID")
        dce_endpoint = _required("BILLING_DCE_ENDPOINT")
        dcr_immutable_id = _required("BILLING_DCR_IMMUTABLE_ID")
        stream_name = os.environ.get("BILLING_STREAM_NAME") or "Custom-FoundryBilledCost_CL"

        raw_lookback = os.environ.get("BILLING_LOOKBACK_DAYS")
        if raw_lookback:
            try:
                lookback = int(raw_lookback)
            except ValueError as exc:
                raise ConfigError("BILLING_LOOKBACK_DAYS must be an integer.") from exc
            if not 1 <= lookback <= 90:
                raise ConfigError("BILLING_LOOKBACK_DAYS must be between 1 and 90.")
        else:
            lookback = _DEFAULT_LOOKBACK_DAYS

        account = os.environ.get("BILLING_ACCOUNT_NAME") or None

        return cls(
            subscription_id=subscription_id,
            dce_endpoint=dce_endpoint,
            dcr_immutable_id=dcr_immutable_id,
            stream_name=stream_name,
            lookback_days=lookback,
            account_name=account,
        )


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is required.")
    return value
