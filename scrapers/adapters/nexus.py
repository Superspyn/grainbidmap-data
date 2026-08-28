"""Adapter for Nexus Cooperative's cash-bid page.

Nexus renders every location server-side on one page, so a single request gets
the lot:

    section.location
      h1                     -> location name ("Rockford, IA")
      div.cashbids_table
        h1                   -> commodity ("Corn")
        div.table_wrapper > table

Every table carries the same header row - Delivery Start, Delivery End, Basis
Month, Futures Price, Basis, Cash Price, Futures Change - so columns are looked
up by name rather than position.

Two things about this feed are worth knowing:

**The futures change carries its sign in a CSS class, not the text.** A cell
reads ``<td class="pos">0.0475</td>``. Reading the text alone would publish a
down day as an up day. Every cell on the page was ``pos`` when this was
written, and the archived copies do not include the bid tables, so the negative
rendering has never been observed - ``_signed_change`` therefore honours an
explicit sign in the text if there is one and otherwise falls back to the
class, which is correct whichever way they render it.

**Nexus quotes delivered bids to other companies' plants** - AGP Manning,
Cargill Iowa Falls, Valero Charles City, POET Fairbank. Those are Nexus's bids
for grain delivered there, not those plants' own posted bids, so they must
never land on an AGP or Cargill pin. match_locations.py scopes candidates by
the pin's company, which already prevents it.

robots.txt allows /cash-bids/ (it disallows /calendar/action*, /events/action*
and anything with a query string) and asks for a 3 second crawl delay. One
request per run satisfies both.
"""

from __future__ import annotations

import datetime as _dt

from bs4 import BeautifulSoup

import fetch
import normalize
from adapters.base import Bid, SourceLocation

URL = "https://www.nexus.coop/cash-bids/"

# Class names a table cell might use to mark a negative number. Only "pos" has
# ever been seen live; the rest are the obvious counterparts, kept so a down day
# cannot silently publish as an up day.
NEGATIVE_CLASSES = {"neg", "negative", "down", "minus", "lower"}


class NexusAdapter:
    name = "nexus"

    def fetch(self) -> list[SourceLocation]:
        body = fetch.get(URL, browser_ua=True)
        return self.parse(body)

    @classmethod
    def parse(cls, body: str) -> list[SourceLocation]:
        soup = BeautifulSoup(body, "html.parser")
        as_of = _now()

        locations: list[SourceLocation] = []
        for index, section in enumerate(soup.select("section.location")):
            heading = section.find("h1")
            name = heading.get_text(" ", strip=True) if heading else ""
            if not name:
                continue

            bids: list[Bid] = []
            for block in section.select(".cashbids_table"):
                label = block.find("h1")
                grain = normalize.classify_grain(
                    label.get_text(" ", strip=True) if label else None
                )
                if grain is None:
                    continue
                table = block.find("table")
                if table is not None:
                    bids.extend(cls._parse_table(table, grain))

            if not bids:
                continue

            bids.sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
            locations.append(
                SourceLocation(
                    # The page exposes no id, so the name is the only stable
                    # handle. Index keeps it unique if two ever share a name.
                    source_location_id=f"{index}:{name}",
                    name=name,
                    bids=bids,
                    as_of=as_of,
                )
            )

        if not locations:
            raise ValueError("nexus: no corn or soybean bids found")
        return locations

    @staticmethod
    def _parse_table(table, grain: str) -> list[Bid]:
        rows = table.find_all("tr")
        if not rows:
            return []
        header = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]

        out: list[Bid] = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells:
                continue
            by_name = dict(zip(header, cells))

            def text(column: str) -> str | None:
                cell = by_name.get(column)
                return cell.get_text(" ", strip=True) if cell is not None else None

            cash = normalize.parse_money(text("Cash Price"))
            if cash is None:
                continue

            start = normalize.parse_date(text("Delivery Start"))
            end = normalize.parse_date(text("Delivery End"))
            basis_month = text("Basis Month")

            out.append(
                Bid(
                    grain=grain,
                    delivery_start=start,
                    delivery_end=end,
                    delivery_label=normalize.format_delivery_label(start, end),
                    futures_month=normalize.futures_month_code(grain, basis_month),
                    futures=normalize.parse_money(text("Futures Price")),
                    futures_change=_signed_change(by_name.get("Futures Change")),
                    basis=normalize.parse_money(text("Basis")),
                    cash=cash,
                )
            )
        return out


def _signed_change(cell) -> float | None:
    """Read a futures change whose sign may live in the cell's class.

    ``<td class="pos">0.0475</td>`` is +0.0475. An explicit sign in the text
    wins if present; otherwise a negative-looking class flips it.
    """
    if cell is None:
        return None
    text = cell.get_text(" ", strip=True)
    value = normalize.parse_money(text)
    if value is None:
        return None
    if text.lstrip().startswith(("-", "+")):
        return value
    classes = {c.lower() for c in (cell.get("class") or [])}
    if classes & NEGATIVE_CLASSES:
        return -abs(value)
    return value


def _now() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
