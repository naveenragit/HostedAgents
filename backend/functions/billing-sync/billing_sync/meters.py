"""Meter-name to model-deployment mapping.

Billing meter names do not match model deployment names, so the mapping is data
rather than code. It is shared with ``scripts/sync-model-prices.ps1`` through
``observability/model-meter-map.json``; the deploy script copies that file into
this package so the container image carries its own copy (the build context is
only this directory).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

_PACKAGED_MAP: Final[Path] = Path(__file__).with_name("model-meter-map.json")
_REPO_MAP_RELATIVE: Final[tuple[str, ...]] = ("observability", "model-meter-map.json")

UNMAPPED_MODEL: Final[str] = "unmapped"


def _repo_map() -> Path | None:
    """Locate the committed map when running from a checkout rather than the image.

    Searched by walking up rather than indexing a fixed parent, because the
    container flattens the tree to /app and a fixed index raises IndexError at
    import time.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent.joinpath(*_REPO_MAP_RELATIVE)
        if candidate.exists():
            return candidate
    return None


def _map_file() -> Path:
    """Return the meter map, preferring the copy packaged at build time.

    Only the service directory is in the build context, so the image carries its
    own copy. Locally that copy is absent and the committed source is used, which
    keeps exactly one file under version control.
    """
    if _PACKAGED_MAP.exists():
        return _PACKAGED_MAP
    repo_map = _repo_map()
    if repo_map is not None:
        return repo_map
    raise FileNotFoundError(f"Meter map not found at {_PACKAGED_MAP} or in a repo checkout.")


@dataclass(frozen=True)
class MeterMapping:
    model: str
    direction: str
    tier: str


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    mapping: MeterMapping


@lru_cache(maxsize=1)
def _rules() -> tuple[_Rule, ...]:
    raw = json.loads(_map_file().read_text(encoding="utf-8"))
    return tuple(
        _Rule(
            pattern=re.compile(entry["pattern"]),
            mapping=MeterMapping(
                model=entry["model"],
                direction=entry["direction"],
                tier=entry["tier"],
            ),
        )
        for entry in raw["meters"]
    )


def resolve(meter_name: str) -> MeterMapping | None:
    """Return the mapping for ``meter_name``, or None when it is unmapped."""
    for rule in _rules():
        if rule.pattern.search(meter_name):
            return rule.mapping
    return None
