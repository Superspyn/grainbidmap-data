"""Adapter for Heartland Co-op's closing bid sheet.

Heartland serves every one of its delivery points from a single server-rendered
page at ``myaccount.heartlandcoop.com/bids.htm`` - no JavaScript, no API. The
page holds four tables (regular and processor, corn and soybeans). Column
headers carry the delivery window plus the futures month; each cell holds two
spans, cash price then basis.

Heartland publishes cash and basis but not the futures price, so futures is
derived as ``cash - basis`` - the inverse of what the AgriCharts adapter does.
"""

from __future__ import annotations

import datetime as _dt
import re

from bs4 import BeautifulSoup

import fetch
import normalize
from adapters.base import Bid, SourceLocation

URL = "https://myaccount.heartlandcoop.com/bids.htm"

# "CU26" / "SX26" - grain letter, month code, two-digit year.
_FUTURES_RE = re.compile(r"([A-Z]{1,2}[FGHJKMNQUVXZ]\d{2})")


class HeartlandAdapter:
    name = "heartland"

    def fetch(self) -> list[SourceLocation]:
        return self.parse(fetch.get(URL, browser_ua=True))

    def parse(self, html: str) -> list[SourceLocation]:
        soup = BeautifulSoup(html, "html.parser")
        as_of = self._parse_as_of(soup)

        # Keyed by (is_processor, name): a co-op elevator and a processor can
        # share a town name but are genuinely different delivery points.
        collected: dict[tuple[bool, str], SourceLocation] = {}

        for table in soup.find_all("table"):
            headers = table.find_all("th")
            if not headers:
                continue
            heading = headers[0].get_text(strip=True).upper()
            grain = normalize.classify_grain(heading)
            if grain is None:
                continue
            is_processor = "PROCESSOR" in heading

            columns = [self._parse_column(th) for th in headers[1:]]

            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                name = cells[0].get_text(strip=True)
                if not name:
                    continue

                key = (is_processor, name.upper())
                location = collected.get(key)
                if location is None:
                    location = SourceLocation(
                        source_location_id=("processor:" if is_processor else "") + name.upper(),
                        name=name,
                        state="IA",
                        as_of=as_of,
                    )
                    collected[key] = location

                for column, cell in zip(columns, cells[1:]):
                    bid = self._parse_cell(cell, column, grain)
                    if bid is not None:
                        location.bids.append(bid)

        locations = [loc for loc in collected.values() if loc.bids]
        if not locations:
            raise ValueError("heartland: no corn or soybean bids found on the page")
        for loc in locations:
            loc.bids.sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
        return locations

    @staticmethod
    def _parse_column(th) -> dict:
        span = th.find("span")
        start = normalize.parse_date(span.get("data-start")) if span else None
        end = normalize.parse_date(span.get("data-end")) if span else None
        match = _FUTURES_RE.search(th.get_text(strip=True).upper())
        return {
            "start": start,
            "end": end,
            "label": normalize.format_delivery_label(start, end),
            "futures_month": match.group(1) if match else None,
        }

    @staticmethod
    def _parse_cell(cell, column: dict, grain: str) -> Bid | None:
        spans = cell.find_all("span")
        if len(spans) < 2:
            return None
        cash = normalize.parse_money(spans[0].get_text(strip=True))
        basis = normalize.parse_money(spans[1].get_text(strip=True))
        if cash is None:
            return None
        return Bid(
            grain=grain,
            delivery_start=column["start"],
            delivery_end=column["end"],
            delivery_label=column["label"],
            futures_month=column["futures_month"],
            futures=round(cash - basis, 4) if basis is not None else None,
            futures_change=None,   # not published on this page
            basis=basis,
            cash=cash,
        )

    @staticmethod
    def _parse_as_of(soup) -> str | None:
        """Read the date stamp out of the "CLOSING GRAIN BIDS 82626" heading.

        The digits run together with no separators, so a 5-digit stamp is
        M-DD-YY and a 6-digit one is MM-DD-YY. Anything unexpected falls back to
        the fetch time rather than inventing a date.
        """
        header = soup.select_one("div.header")
        if header:
            digits = re.search(r"\b(\d{5,6})\b", header.get_text(" ", strip=True))
            if digits:
                raw = digits.group(1)
                stamp = raw.zfill(6)
                try:
                    date = _dt.datetime.strptime(stamp, "%m%d%y").date()
                    # Heartland states these are the 1:15 PM CT close.
                    return f"{date.isoformat()}T18:15:00Z"
                except ValueError:
                    pass
        return (
            _dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
