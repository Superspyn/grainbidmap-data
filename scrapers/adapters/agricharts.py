"""Adapter for AgriCharts (Barchart) cash-bid widgets.

Most Iowa co-ops publish bids through an AgriCharts widget. The widget's public
loader hands back a JavaScript file containing a ``var bids = [...]`` literal
with every delivery point and every bid row for that co-op - including
latitude/longitude, which is what makes automatic pin matching reliable.

One request per co-op returns the entire bid table, so this adapter never needs
to touch a per-location page.
"""

from __future__ import annotations

import json
import re
import datetime as _dt

import fetch
import normalize
from adapters.base import Bid, SourceLocation

ENDPOINT = (
    "https://{tenant}.agricharts.com/inc/cashbids/cashbids-js.php"
    "?filter=all&groupby=location&months={months}&format=csv"
)

_BIDS_RE = re.compile(r"var\s+bids\s*=\s*(\[.*?\])\s*;", re.S)
_CONFIG_RE = re.compile(r"var\s+config\s*=\s*(\{.*?\})\s*;\s*var\s+domain", re.S)

# The widget's own `price_calculations` flag says which of price and basis the
# feed treats as authoritative. Every co-op here is mode 0; Cargill is mode 2,
# and the two need opposite handling. See _parse_bids.
_MODE_BASIS_DRIVEN = 2


class AgriChartsAdapter:
    def __init__(self, tenant: str, *, months: int = 12, referer: str | None = None):
        self.tenant = tenant
        self.months = months
        self.referer = referer
        self.name = f"agricharts:{tenant}"

    def fetch(self) -> list[SourceLocation]:
        url = ENDPOINT.format(tenant=self.tenant, months=self.months)
        body = fetch.get(url, referer=self.referer, browser_ua=True)
        return self.parse(body)

    def parse(self, body: str) -> list[SourceLocation]:
        match = _BIDS_RE.search(body)
        if not match:
            raise ValueError(f"{self.name}: no 'var bids = [...]' payload in response")
        raw_locations = json.loads(match.group(1))

        config_match = _CONFIG_RE.search(body)
        try:
            mode = json.loads(config_match.group(1)).get("price_calculations")
        except (AttributeError, json.JSONDecodeError):
            mode = None  # Absent config: fall back to the common mode.

        locations: list[SourceLocation] = []
        for raw in raw_locations:
            if raw.get("hide_on_sites_and_apis") == "1":
                continue
            bids, newest = self._parse_bids(raw.get("cashbids") or [], mode)
            if not bids:
                continue
            locations.append(
                SourceLocation(
                    source_location_id=str(raw.get("id") or ""),
                    name=(raw.get("name") or raw.get("display_name") or "").strip(),
                    city=(raw.get("city") or None),
                    state=(raw.get("state") or None),
                    latitude=_as_float(raw.get("latitude")),
                    longitude=_as_float(raw.get("longitude")),
                    bids=bids,
                    as_of=newest,
                )
            )
        if not locations:
            raise ValueError(f"{self.name}: payload contained no corn or soybean bids")
        return locations

    def _parse_bids(self, rows: list[dict], mode=None) -> tuple[list[Bid], str | None]:
        bids: list[Bid] = []
        newest_ts = 0

        for row in rows:
            if not row.get("active", True):
                continue
            # Bids behind a customer login are intentionally skipped rather
            # than worked around.
            if row.get("require_login"):
                continue

            grain = normalize.classify_grain(
                row.get("sym_root"), row.get("symbol"), row.get("name")
            )
            if grain is None:
                continue

            futures = normalize.parse_tick(row.get("futures") or row.get("futuresprice"))

            if mode == _MODE_BASIS_DRIVEN:
                # Cargill's feed. Here `basis` is the real, location-specific
                # number in DOLLARS, and the price fields are not a bid at all:
                # every location carries the same rounded board price, so
                # deriving basis from them yields ~0 everywhere. The bid is
                # futures + basis.
                basis = _as_float(row.get("basis"))
                if basis is None or futures is None:
                    continue
                cash = round(futures + basis, 4)
            else:
                # Every co-op feed. `price` is the real bid and the basis field
                # is in cents, so derive basis rather than guess at its scale -
                # the subtraction reproduces it exactly.
                cash = normalize.parse_money(
                    row.get("cashpricebushel")
                    or row.get("cashprice")
                    or row.get("price")
                )
                if cash is None:
                    continue
                basis = round(cash - futures, 4) if futures is not None else None

            start = normalize.parse_date(
                row.get("delivery_start_raw") or row.get("delivery_start")
            )
            end = normalize.parse_date(
                row.get("delivery_end_raw") or row.get("delivery_end")
            )

            bids.append(
                Bid(
                    grain=grain,
                    delivery_start=start,
                    delivery_end=end,
                    delivery_label=normalize.format_delivery_label(start, end),
                    futures_month=normalize.futures_month_from_symbol(row.get("symbol")),
                    futures=futures,
                    futures_change=normalize.parse_tick(row.get("futureschange")),
                    basis=basis,
                    cash=cash,
                )
            )

            ts = row.get("timestamp")
            if isinstance(ts, (int, float)) and ts > newest_ts:
                newest_ts = int(ts)

        bids.sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
        as_of = (
            _dt.datetime.fromtimestamp(newest_ts, _dt.timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
            if newest_ts
            else None
        )
        return bids, as_of


def _as_float(value) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None
