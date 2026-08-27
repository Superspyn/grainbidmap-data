"""Adapter for CI Hedging's cash-bid widget.

Sites embed a widget that POSTs to CI Hedging and renders the HTML it returns:

    POST www.cihedging.com/cih/api/index.cfm/v2/origination/cashbids/<companyID>/widget
        -> JSON string containing the table markup

The company id comes from the embedding page: look for ``companyID:`` in the
widget's ``tableOptions`` block. Golden Grain Energy is 98951.

The markup is well attributed - a ``div[data-commodity-name]`` wraps each
commodity's table, and every row carries ``data-delivery-year`` and
``data-delivery-month`` - so nothing here depends on column position or on
parsing display text.

One company is one delivery point in this widget, so a single request covers it.
"""

from __future__ import annotations

import datetime as _dt
import json
import re

from bs4 import BeautifulSoup

import fetch
import normalize
from adapters.base import Bid, SourceLocation

WIDGET_URL = (
    "https://www.cihedging.com/cih/api/index.cfm/v2/origination/cashbids/"
    "{company}/widget?commodity_ids=&custom_commodity_ids="
    "&exclude_non_custom=false&exclude_custom=false&address_ids="
    "&show_cash_bid_title=true&show_cash_bid_filters=false"
    "&show_cash_bid_note=true&show_location_names=true&with_new_chart=true"
)

# "Sep 26 5.1200" - contract month, two-digit year, then the futures price.
_FUTURES_RE = re.compile(
    r"^\s*([A-Za-z]{3,})\w*\s+(\d{2})\s+([\d.]+)\s*$"
)


class CIHedgingAdapter:
    def __init__(self, company_id: str | int, label: str, referer: str | None = None):
        self.company_id = str(company_id)
        self.label = label
        self.referer = referer
        self.name = f"cihedging:{company_id}"

    def fetch(self) -> list[SourceLocation]:
        body = fetch.get(
            WIDGET_URL.format(company=self.company_id),
            referer=self.referer,
            impersonate=True,
            method="POST",
        )
        return self.parse(body, self.company_id, self.label)

    @classmethod
    def parse(cls, body: str, company_id: str, label: str) -> list[SourceLocation]:
        # The endpoint returns the markup as a JSON string.
        try:
            markup = json.loads(body)
        except json.JSONDecodeError:
            markup = body
        if not isinstance(markup, str):
            raise ValueError(f"cihedging:{company_id}: unexpected payload type")

        soup = BeautifulSoup(markup, "html.parser")
        bids: list[Bid] = []

        for block in soup.find_all(attrs={"data-commodity-name": True}):
            grain = normalize.classify_grain(block.get("data-commodity-name"))
            if grain is None:
                continue
            for row in block.find_all("tr"):
                bid = cls._parse_row(row, grain)
                if bid is not None:
                    bids.append(bid)

        if not bids:
            raise ValueError(f"cihedging:{company_id}: no corn or soybean bids found")

        bids.sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
        return [
            SourceLocation(
                source_location_id=str(company_id),
                name=label,
                bids=bids,
                as_of=_now(),
            )
        ]

    @staticmethod
    def _parse_row(row, grain: str) -> Bid | None:
        cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
        if len(cells) < 5:
            return None

        cash = normalize.parse_money(cells[4])
        if cash is None:
            return None
        basis = normalize.parse_money(cells[3])

        futures = futures_month = None
        m = _FUTURES_RE.match(cells[1])
        if m:
            month_name, year2, price = m.groups()
            futures = normalize.parse_money(price)
            futures_month = normalize.futures_month_code(grain, f"{month_name} 20{year2}")

        # Delivery comes from row attributes rather than the printed label.
        start = end = None
        year, month = row.get("data-delivery-year"), row.get("data-delivery-month")
        if year and month:
            start, end = normalize.month_bounds(
                f"{_dt.date(int(year), int(month), 1):%b %Y}"
            )

        return Bid(
            grain=grain,
            delivery_start=start,
            delivery_end=end,
            delivery_label=(row.get("data-delivery-period-label") or "").strip()
            or normalize.format_delivery_label(start, end),
            futures_month=futures_month,
            futures=futures,
            futures_change=normalize.parse_money(cells[2]),
            basis=basis,
            cash=cash,
        )


def _now() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
