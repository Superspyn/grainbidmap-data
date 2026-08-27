"""Adapter for Landus Cooperative.

Landus runs a React front end backed by two small JSON endpoints on their own
site:

    GET /api/locations               -> [{locationName, locationNumber, state}]
    GET /api/cash-bids?location=109  -> {asOfDateTime, cashBids: [{commodity, bids: [...]}]}

Each bid carries ``deliveryDate`` ("Aug 2026"), ``basisMonth`` ("Sep 2026"),
``basisPrice``, ``currentBid`` and ``bidChange``. There is no futures column, so
it is derived as ``currentBid - basisPrice``.

Unlike every other source, Landus has no bulk endpoint - bids are per location,
so this adapter makes 1 + N requests. ``fetch.get`` throttles per host, which
keeps that polite; a location that fails is skipped rather than failing the
whole source, since 50 good locations beat none.
"""

from __future__ import annotations

import datetime as _dt
import json

import fetch
import normalize
from adapters.base import Bid, SourceLocation

LOCATIONS_URL = "https://www.landus.ag/api/locations"
BIDS_URL = "https://www.landus.ag/api/cash-bids?location={id}"
REFERER = "https://www.landus.ag/businesses/grain/grain-bids"


class LandusAdapter:
    name = "landus"

    def fetch(self) -> list[SourceLocation]:
        raw = fetch.get(LOCATIONS_URL, referer=REFERER, impersonate=True)
        try:
            directory = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"landus: /api/locations was not JSON: {exc}") from exc

        locations: list[SourceLocation] = []
        failures = 0

        for entry in directory:
            number = str(entry.get("locationNumber") or "").strip()
            name = (entry.get("locationName") or "").strip()
            if not number or not name:
                continue
            try:
                body = fetch.get(
                    BIDS_URL.format(id=number), referer=REFERER, impersonate=True
                )
                bids, as_of = self.parse_location(body)
            except Exception:      # noqa: BLE001 - one bad location must not sink the rest
                failures += 1
                continue
            if not bids:
                continue
            locations.append(
                SourceLocation(
                    source_location_id=number,
                    name=name,
                    state=(entry.get("state") or None),
                    bids=bids,
                    as_of=as_of,
                )
            )

        if not locations:
            raise ValueError(
                f"landus: no locations returned bids ({failures} request failures)"
            )
        return locations

    @staticmethod
    def parse_location(body: str) -> tuple[list[Bid], str | None]:
        data = json.loads(body)
        bids: list[Bid] = []

        for group in data.get("cashBids") or []:
            grain = normalize.classify_grain(group.get("commodity"))
            if grain is None:
                continue

            for row in group.get("bids") or []:
                cash = normalize.parse_money(row.get("currentBid"))
                if cash is None:
                    continue
                basis = normalize.parse_money(row.get("basisPrice"))
                # No futures column is published; it is implied by the pair.
                futures = round(cash - basis, 4) if basis is not None else None

                start, end = normalize.month_bounds(row.get("deliveryDate"))
                bids.append(
                    Bid(
                        grain=grain,
                        delivery_start=start,
                        delivery_end=end,
                        delivery_label=normalize.format_delivery_label(start, end),
                        futures_month=normalize.futures_month_code(
                            grain, row.get("basisMonth")
                        ),
                        futures=futures,
                        futures_change=normalize.parse_money(row.get("bidChange")),
                        basis=basis,
                        cash=cash,
                    )
                )

        bids.sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
        return bids, _parse_as_of(data.get("asOfDateTime"))


def _parse_as_of(stamp: str | None) -> str | None:
    """``"08/27/2026 11:11 AM"`` (Central) -> ISO 8601 UTC.

    Landus stamps its own publish time in local co-op time with no zone. Falls
    back to now() rather than inventing an offset if the shape is unexpected.
    """
    now = (
        _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    if not stamp:
        return now
    try:
        naive = _dt.datetime.strptime(str(stamp).strip(), "%m/%d/%Y %I:%M %p")
    except ValueError:
        return now
    try:
        from zoneinfo import ZoneInfo

        central = naive.replace(tzinfo=ZoneInfo("America/Chicago"))
    except Exception:
        # tzdata missing (common on bare Windows). CDT is the safer assumption
        # than pretending the stamp is UTC, which would be 5-6 hours out.
        central = naive.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=-5)))
    return (
        central.astimezone(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
