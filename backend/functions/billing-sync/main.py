"""Entry point for the daily billed-cost sync.

Runs as a one-shot Container Apps job rather than a Function App: every storage
account in this subscription is governed to private-only access with shared keys
disabled, and a Function App cannot start without reachable storage. A job needs
no storage account at all, so the constraint disappears instead of being worked
around with a VNet and private endpoints.

Keyless throughout. The job's user-assigned identity needs Cost Management Reader
on the subscription and Monitoring Metrics Publisher on the data collection rule.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta

from azure.identity import DefaultAzureCredential
from azure.monitor.ingestion import LogsIngestionClient

from billing_sync.config import ConfigError, SyncConfig
from billing_sync.costmanagement import fetch_usage_records
from billing_sync.usage import aggregate, to_log_entries

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, stream=sys.stdout, force=True)
    # The Azure SDKs log request detail at INFO, which buries the run summary.
    logging.getLogger("azure").setLevel(logging.WARNING)
    logger = logging.getLogger("billing_sync")

    try:
        config = SyncConfig.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    since = (datetime.now(UTC) - timedelta(days=config.lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    credential = DefaultAzureCredential()
    try:
        rows = aggregate(
            fetch_usage_records(credential, config.subscription_id),
            since=since,
            subscription_id=config.subscription_id,
            account_name=config.account_name,
        )
        if not rows:
            # Not a failure. A subscription with no model traffic in the window
            # legitimately has nothing to bill.
            logger.info("no billed model meters in the last %d day(s)", config.lookback_days)
            return 0

        client = LogsIngestionClient(endpoint=config.dce_endpoint, credential=credential)
        with client:
            client.upload(
                rule_id=config.dcr_immutable_id,
                stream_name=config.stream_name,
                logs=to_log_entries(rows),
            )

        logger.info(
            "ingested %d row(s) covering %.6f USD across %d model(s)",
            len(rows),
            sum(row.billed_cost_usd for row in rows),
            len({row.model_name for row in rows}),
        )
        return 0
    finally:
        credential.close()


if __name__ == "__main__":
    raise SystemExit(main())
