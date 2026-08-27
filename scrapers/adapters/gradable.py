"""Adapter for Gradable (FBN) merchandising sites.

POET and ADM both run Gradable, at ``poet.gradable.com`` and
``adm.gradable.com``, so this adapter takes the tenant subdomain.

Two endpoints:

    GET /api/commodities/merchandising/bootstrap
        -> {"markets": [{id, display_name, num_public_instruments, ...}]}

    GET /api/commodities/v2/merchandising/instruments/market/<id>?offer_type=public
        -> {"crops": [...], "instruments": [{cash_bid, basis_bid, futures_bid, ...}]}

One bootstrap call enumerates every market, then one call per market that
actually publishes public bids.

Responses are prefixed with ``while(1);`` - a standard guard against JSON
hijacking - which has to be stripped before parsing.
"""

from __future__ import annotations

import datetime as _dt
import json
import re

import fetch
import normalize
from adapters.base import Bid, SourceLocation

BOOTSTRAP_PATH = "/api/commodities/merchandising/bootstrap"
COMMODITIES_PATH = "/api/commodities/profit-center/commodities"
INSTRUMENTS_PATH = (
    "/api/commodities/v2/merchandising/instruments/market/{id}?offer_type=public"
)

_XSSI_PREFIX = re.compile(r"^\s*while\(1\);\s*")


def _load(body: str):
    return json.loads(_XSSI_PREFIX.sub("", body))


class GradableAdapter:
    def __init__(self, tenant: str):
        self.tenant = tenant
        self.base = f"https://{tenant}.gradable.com"
        self.name = f"gradable:{tenant}"

    def fetch(self) -> list[SourceLocation]:
        referer = self.base + "/"
        boot = _load(fetch.get(self.base + BOOTSTRAP_PATH, referer=referer, impersonate=True))
        grain_for_id = self._commodity_map(referer)

        locations: list[SourceLocation] = []
        for market in boot.get("markets") or []:
            if not market.get("enable_gradable_public_site"):
                continue
            if not market.get("num_public_instruments"):
                continue
            market_id = market.get("id")
            name = (market.get("display_name") or "").strip()
            if not market_id or not name:
                continue

            try:
                payload = _load(
                    fetch.get(
                        self.base + INSTRUMENTS_PATH.format(id=market_id),
                        referer=referer,
                        impersonate=True,
                    )
                )
            except Exception:      # noqa: BLE001 - one market must not sink the tenant
                continue

            bids = self.parse_instruments(payload, grain_for_id)
            if not bids:
                continue

            city, _, state = name.partition(",")
            locations.append(
                SourceLocation(
                    source_location_id=str(market_id),
                    name=name,
                    city=city.strip() or None,
                    state=state.strip() or None,
                    bids=bids,
                    as_of=_now(),
                )
            )

        if not locations:
            raise ValueError(f"{self.name}: no markets returned corn or soybean bids")
        return locations

    def _commodity_map(self, referer: str) -> dict[int, str]:
        """commodity_id -> "corn"/"soybeans", read from the tenant's own list.

        Hard-coding 1 and 2 would work today, but the ids are Gradable's and
        this costs one request.
        """
        try:
            rows = _load(
                fetch.get(self.base + COMMODITIES_PATH, referer=referer, impersonate=True)
            )
        except Exception:      # noqa: BLE001
            return {}
        out: dict[int, str] = {}
        for row in rows or []:
            grain = normalize.classify_grain(row.get("name"), row.get("fbn_code"))
            if grain and row.get("id") is not None:
                out[row["id"]] = grain
        return out

    @staticmethod
    def parse_instruments(payload: dict, grain_for_id: dict[int, str]) -> list[Bid]:
        bids: list[Bid] = []

        for row in payload.get("instruments") or []:
            if row.get("deleted"):
                continue

            grain = grain_for_id.get(row.get("commodity_id"))
            if grain is None:
                # Fall back to the codes on the row itself if the lookup failed.
                grain = normalize.classify_grain(
                    row.get("ext_commodity_id"), row.get("option_month")
                )
            if grain is None:
                continue

            cash = normalize.parse_money(row.get("cash_bid"))
            if cash is None:
                continue

            start = _epoch_date(row.get("delivery_period_start"))
            end = _epoch_date(row.get("delivery_period_end"))

            bids.append(
                Bid(
                    grain=grain,
                    delivery_start=start,
                    delivery_end=end,
                    # Gradable's own label ("August 2026") is friendlier than a
                    # derived one when the window is an odd shape.
                    delivery_label=(row.get("display_name") or "").strip()
                    or normalize.format_delivery_label(start, end),
                    futures_month=normalize.expand_futures_symbol(
                        row.get("option_month"), int(start[:4]) if start else None
                    ),
                    futures=normalize.parse_money(row.get("futures_bid")),
                    futures_change=None,   # not published on this endpoint
                    basis=normalize.parse_money(row.get("basis_bid")),
                    cash=cash,
                )
            )

        bids.sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
        return bids


def _epoch_date(value) -> str | None:
    try:
        return (
            _dt.datetime.fromtimestamp(int(value), _dt.timezone.utc).date().isoformat()
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _now() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
