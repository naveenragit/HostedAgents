"""Create the database, collections and indexes, then verify connectivity.

Run via `scripts/init-database.ps1`. Deliberately reuses `MongoResources` so the
provisioning path and the application's start-up path cannot diverge.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.config import ConfigError, get_settings
from app.repositories.mongo import MongoResources
from app.security.credentials import build_azure_credential

_LOGGER = logging.getLogger("api.init")

# backend/api/.env — resolved from this file so the script works from any cwd.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


async def _run() -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    credential = build_azure_credential(settings)
    resources = MongoResources(
        connection_string=settings.mongo_connection_string,
        credential=credential,
        database_name=settings.mongo_database,
        conversations_collection=settings.conversations_collection,
        messages_collection=settings.messages_collection,
        turns_collection=settings.turns_collection,
        deletion_tombstone_retention_seconds=(settings.deletion_tombstone_retention_seconds),
    )

    try:
        await resources.ping()
        _LOGGER.info("connected")

        await resources.ensure_schema()

        names = sorted(await resources.database.list_collection_names())
        _LOGGER.info(
            "database ready",
            extra={"database": settings.mongo_database, "collections": ",".join(names)},
        )
        print(  # noqa: T201
            f"Database '{settings.mongo_database}' is ready. Collections: {', '.join(names)}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        # The message can name the cluster, which is useful here and safe: this
        # runs interactively, not in a request handler.
        print(f"Initialisation failed: {type(exc).__name__}: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    finally:
        await resources.close()
        credential.close()


def main() -> int:
    load_dotenv(_ENV_FILE, override=False)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s %(message)s")
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
