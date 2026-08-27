"""The contract every bid source implements."""

from __future__ import annotations

import dataclasses
from typing import Protocol


@dataclasses.dataclass
class Bid:
    """One row of a cash-bid table, normalised to USD per bushel."""

    grain: str                      # "corn" | "soybeans"
    delivery_start: str | None      # ISO date
    delivery_end: str | None        # ISO date
    delivery_label: str | None      # "Oct/Nov 2026"
    futures_month: str | None       # "CZ26"
    futures: float | None
    futures_change: float | None
    basis: float | None
    cash: float | None

    def as_dict(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


@dataclasses.dataclass
class SourceLocation:
    """A delivery point at a source, with whatever identity it exposes.

    ``latitude``/``longitude`` are optional because only some feeds publish
    them; ``match_locations.py`` falls back to name matching without them.
    """

    source_location_id: str
    name: str
    bids: list[Bid] = dataclasses.field(default_factory=list)
    city: str | None = None
    state: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    as_of: str | None = None        # ISO 8601 UTC


class Adapter(Protocol):
    """Adapters fetch one source and return all of its delivery points.

    Implementations must issue as few requests as the source allows - ideally
    exactly one - and must raise on failure rather than returning partial data,
    so ``build_bids.py`` can fall back to the last good result for that source.
    """

    name: str

    def fetch(self) -> list[SourceLocation]:
        ...
