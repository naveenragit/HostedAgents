"""Cost Management retrieval.

Isolated from ``billing_sync.usage`` so the normalization logic stays free of
Azure SDK imports and testable offline. The SDK pager follows ``nextLink``,
which is what makes this reliable: a hand-rolled single-page read silently
truncated older days once the first page filled.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, Final

from azure.core.credentials import TokenCredential
from azure.mgmt.consumption import ConsumptionManagementClient

_LOGGER: Final = logging.getLogger("billing_sync.costmanagement")


def fetch_usage_records(
    credential: TokenCredential,
    subscription_id: str,
) -> Iterator[Any]:
    """Yield actual-cost usage detail records for the subscription."""
    client = ConsumptionManagementClient(credential, subscription_id)
    _LOGGER.info("reading usage details for subscription %s", subscription_id)
    yield from client.usage_details.list(
        scope=f"/subscriptions/{subscription_id}",
        metric="ActualCost",
    )
