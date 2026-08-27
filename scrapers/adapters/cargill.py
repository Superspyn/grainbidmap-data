"""Adapter for Cargill Ag Horizons.

Two endpoints, both public and JSON:

    GET api.cglcloud.com/api/dxo/ag/v1/prices/locations
        -> {"locations": [{id, name, state, latitude, longitude, basisLocationId}]}

    GET d96y3rjfk5o7l.cloudfront.net/?module=cashbids&output=json
        &countryCode=US&location=<id>,<id>,...&commOverviewByLocation=1
        -> {"bigGroups": [{name, symbol, cashbids: [...]}]}

The bids endpoint accepts a comma-separated list of ``basisLocationId`` values,
so this costs a handful of batched requests rather than one per elevator.
Batches are capped well under the point where the service starts returning
nothing (19+ locations comes back empty).

Rows carry ``flatprice`` (cash), ``futuresprice`` and ``basis`` in dollars, so
nothing needs deriving - but the three are cross-checked, since a silent unit
change here would be hard to spot.
"""

from __future__ import annotations

import datetime as _dt
import json

import fetch
import normalize
from adapters.base import Bid, SourceLocation

LOCATIONS_URL = "https://api.cglcloud.com/api/dxo/ag/v1/prices/locations"
BIDS_URL = (
    "https://d96y3rjfk5o7l.cloudfront.net/"
    "?countryCode=US&commRoots=&module=cashbids&output=json"
    "&commOverviewByLocation=1&location={ids}"
)
REFERER = "https://www.cargillag.com/check-prices"

# 19 locations in one call returns an empty payload; stay well under it.
BATCH_SIZE = 12


class CargillAdapter:
    name = "cargill"

    def fetch(self) -> list[SourceLocation]:
        directory = json.loads(fetch.get(LOCATIONS_URL, referer=REFERER, impersonate=True))
        entries = directory.get("locations") or []

        # Only US delivery points quote in the cash-bid feed; the Canadian
        # AGROSOFT ids return nothing and would just waste requests.
        wanted = {
            str(e["basisLocationId"]): e
            for e in entries
            if e.get("basisLocationId") and str(e["basisLocationId"]).isdigit()
        }

        by_location: dict[str, list[Bid]] = {}
        ids = sorted(wanted)
        for start in range(0, len(ids), BATCH_SIZE):
            batch = ids[start:start + BATCH_SIZE]
            body = fetch.get(
                BIDS_URL.format(ids=",".join(batch)), referer=REFERER, impersonate=True
            )
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                continue
            self._collect(payload, by_location)

        as_of = (
            _dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

        locations: list[SourceLocation] = []
        for loc_id, bids in by_location.items():
            entry = wanted.get(loc_id)
            if not entry or not bids:
                continue
            bids.sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
            locations.append(
                SourceLocation(
                    source_location_id=loc_id,
                    name=(entry.get("name") or "").strip(),
                    state=entry.get("state"),
                    latitude=_as_float(entry.get("latitude")),
                    longitude=_as_float(entry.get("longitude")),
                    bids=bids,
                    as_of=as_of,
                )
            )

        if not locations:
            raise ValueError("cargill: no corn or soybean bids returned")
        return locations

    @classmethod
    def _collect(cls, payload: dict, out: dict[str, list[Bid]]) -> None:
        for group in payload.get("bigGroups") or []:
            grain = normalize.classify_grain(group.get("name"), group.get("symbol"))
            if grain is None:
                continue

            for row in group.get("cashbids") or []:
                cash = normalize.parse_money(row.get("flatprice"))
                if cash is None:
                    continue
                futures = normalize.parse_money(row.get("futuresprice"))
                basis = normalize.parse_money(row.get("basis"))

                start = normalize.parse_date(row.get("delivery_start"))
                end = normalize.parse_date(row.get("delivery_end"))

                bid = Bid(
                    grain=grain,
                    delivery_start=start,
                    delivery_end=end,
                    delivery_label=normalize.format_delivery_label(start, end),
                    futures_month=normalize.expand_futures_symbol(
                        row.get("futuresymbol"), int(start[:4]) if start else None
                    ) or normalize.futures_month_from_symbol(group.get("symbol")),
                    futures=futures,
                    futures_change=normalize.parse_money(row.get("rawchange")),
                    basis=basis,
                    cash=cash,
                )

                # Cargill publishes all three, so disagreement means a unit or
                # field change upstream rather than a rounding difference.
                if futures is not None and basis is not None:
                    if abs((futures + basis) - cash) > 0.02:
                        continue

                for loc in row.get("locations") or []:
                    loc_id = str(loc.get("id") if isinstance(loc, dict) else loc)
                    if loc_id:
                        out.setdefault(loc_id, []).append(bid)


def _as_float(value) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None
