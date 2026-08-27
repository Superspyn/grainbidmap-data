"""Adapter for NEW Cooperative's cash-bids page.

NEW Co-op used to run an AgriCharts widget - the pins still carry
``?agricharts_loc=`` query strings - but they have moved to their own
server-rendered pages. One request returns a table per delivery point:

    COMMODITY | DELIVERY | CASH PRICE | BASIS | FUTURE PRICE | FUTURE CHANGE
    Corn      | 8/1/2026 - 8/31/2026 | $4.90 | -44 | 534-4 | -2-0

Each table's town name is the nearest preceding heading, so the parser walks
the document in order and remembers the last heading it saw.

The page is behind bot protection that rejects Python's default TLS handshake
with a 403, so this is the one source fetched with ``impersonate=True``.
"""

from __future__ import annotations

import datetime as _dt
import re

from bs4 import BeautifulSoup

import fetch
import normalize
from adapters.base import Bid, SourceLocation

URL = "https://www.newcoop.com/cash-bids/"

# "8/1/2026 - 8/31/2026"
_RANGE_RE = re.compile(r"([\d/]+)\s*[-–]\s*([\d/]+)")

_EXPECTED_HEADERS = {"COMMODITY", "DELIVERY", "CASH PRICE"}


class NewCoopAdapter:
    name = "newcoop"

    def fetch(self) -> list[SourceLocation]:
        return self.parse(fetch.get(URL, impersonate=True))

    def parse(self, html: str) -> list[SourceLocation]:
        soup = BeautifulSoup(html, "html.parser")
        as_of = (
            _dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

        locations: list[SourceLocation] = []
        heading = None

        # Walk headings and tables in document order; each table belongs to the
        # heading most recently seen above it.
        for node in soup.find_all(["h1", "h2", "h3", "table"]):
            if node.name != "table":
                text = node.get_text(" ", strip=True)
                # Town names are short; page furniture ("Cash Bids") is skipped
                # by the bid-table check below rather than guessed at here.
                if text and len(text) < 60:
                    heading = text
                continue

            headers = {th.get_text(strip=True).upper() for th in node.find_all("th")}
            if not _EXPECTED_HEADERS.issubset(headers):
                continue
            if not heading:
                continue

            bids = self._parse_table(node)
            if bids:
                locations.append(
                    SourceLocation(
                        source_location_id=heading.upper(),
                        name=heading,
                        state="IA",
                        bids=bids,
                        as_of=as_of,
                    )
                )

        if not locations:
            raise ValueError("newcoop: no corn or soybean bid tables found")
        return locations

    @staticmethod
    def _parse_table(table) -> list[Bid]:
        bids: list[Bid] = []

        for row in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
            if len(cells) < 3:
                continue

            grain = normalize.classify_grain(cells[0])
            if grain is None:
                continue

            cash = normalize.parse_money(cells[2])
            if cash is None:
                continue

            futures = normalize.parse_tick(cells[4]) if len(cells) > 4 else None
            change = normalize.parse_tick(cells[5]) if len(cells) > 5 else None

            start = end = None
            m = _RANGE_RE.search(cells[1])
            if m:
                start = normalize.parse_date(m.group(1))
                end = normalize.parse_date(m.group(2))
            else:
                start = normalize.parse_date(cells[1])

            # The page prints basis rounded to whole cents; deriving it keeps
            # basis + futures == cash exactly, as with every other source.
            basis = round(cash - futures, 4) if futures is not None else None

            bids.append(
                Bid(
                    grain=grain,
                    delivery_start=start,
                    delivery_end=end,
                    delivery_label=normalize.format_delivery_label(start, end),
                    futures_month=None,   # not published on this page
                    futures=futures,
                    futures_change=change,
                    basis=basis,
                    cash=cash,
                )
            )

        bids.sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
        return bids
