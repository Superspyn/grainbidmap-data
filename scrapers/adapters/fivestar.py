"""Adapter for Five Star Cooperative's cash-bid page.

One page carries every location. The bids live in a wpDataTables table that is
fully server-rendered - the loader skeleton in the markup is decoration - so a
single request gets all of it with no browser:

    table#table_1   Location | Commodity | Delivery Periods | Bids | Basis |
                    Change | LocationID | ... | TradeDateTime | ...
    table#table_2   the CBOT board, one row per contract, with the ticker
                    symbol (``@CZ26``), Last and Change

Three things about this feed need handling.

**There is no futures price or basis month on a bid row.** Basis is in dollars
and the bid is the cash price, so ``futures = cash - basis`` recovers the board
price - but both are rounded to a cent, so the result lands a fraction off the
real contract (5.36 against a 5.365 close). The contract is recovered by
matching that implied price against table_2 within half a cent *and* requiring
the day's change to agree exactly. Either key alone is ambiguous: corn CZ26 and
CH28 both closed at 5.365, and CK27/CN27 sat at 5.570/5.575. Together they
resolve every row, and a row that stays ambiguous simply gets no futures month
rather than a guess.

**The Change column is in cents**, matching table_2, so it is divided by 100 on
the way out to match every other source in this project.

**Five Star quotes delivered bids to other companies' plants** - AGP Mason
City, GGE Mason City, Valero Charles City, Shell Rock Soy Processing, Green
Plains, Homeland, Reicks View Milling, Christensen Farms. Those are Five Star's
bids for grain hauled there, not those plants' own posted bids, and must never
land on an AGP or Valero pin. match_locations.py scopes candidates by the pin's
company, which already prevents it.

Delivery periods are free text and inconsistently written - "Aug 26.", "Dec26",
"Oct 2026.", "April 27", plus "FH Sep" and "LH Aug" for half-months.

robots.txt allows this path (it disallows only /wp-admin/ and preview URLs).
"""

from __future__ import annotations

import calendar as _calendar
import datetime as _dt
import re

from bs4 import BeautifulSoup

import fetch
import normalize
from adapters.base import Bid, SourceLocation

URL = "https://www.fivestarcoop.com/grain-elevators-and-services/five-star-cash-bids/"

BIDS_TABLE = "table_1"
BOARD_TABLE = "table_2"

# Half a cent, which is all the rounding in the bid table can cost us.
FUTURES_TOLERANCE = 0.0076

# "FH" / "LH" prefixes, i.e. first and last half of the month.
_HALF_RE = re.compile(r"^\s*(FH|LH)\s+(.+)$", re.I)
# "Aug 26.", "Dec26", "Oct 2026.", "April 27" - month, optional space, year.
_PERIOD_RE = re.compile(r"^\s*([A-Za-z]{3,9})\.?\s*(\d{2,4})?\.?\s*$")


class FiveStarAdapter:
    name = "fivestar"

    def fetch(self) -> list[SourceLocation]:
        return self.parse(fetch.get(URL, browser_ua=True))

    @classmethod
    def parse(cls, body: str) -> list[SourceLocation]:
        soup = BeautifulSoup(body, "html.parser")
        board = _read_board(soup)
        as_of = _now()

        by_location: dict[str, dict] = {}
        for cells in _rows(soup, BIDS_TABLE):
            if len(cells) < 7:
                continue
            name, commodity, period, bid_text, basis_text, change_text = cells[:6]
            location_id = cells[6]

            grain = normalize.classify_grain(commodity)
            if grain is None:
                continue

            cash = normalize.parse_money(bid_text)
            basis = normalize.parse_money(basis_text)
            if cash is None:
                # Rows for a delivery period this location is not bidding on
                # are left blank rather than omitted.
                continue

            # Cents on this page, dollars everywhere else in this project.
            change = normalize.parse_money(change_text)
            change = round(change / 100.0, 6) if change is not None else None

            futures = round(cash - basis, 4) if basis is not None else None
            start, end = _delivery_window(period)

            entry = by_location.setdefault(
                location_id or name, {"name": name, "bids": []}
            )
            entry["bids"].append(
                Bid(
                    grain=grain,
                    delivery_start=start,
                    delivery_end=end,
                    delivery_label=normalize.format_delivery_label(start, end),
                    futures_month=board.match(grain, futures, change),
                    futures=futures,
                    futures_change=change,
                    basis=basis,
                    cash=cash,
                )
            )

        locations = []
        for location_id, entry in by_location.items():
            entry["bids"].sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
            locations.append(
                SourceLocation(
                    source_location_id=location_id,
                    name=entry["name"],
                    bids=entry["bids"],
                    as_of=as_of,
                )
            )

        if not locations:
            raise ValueError("fivestar: no corn or soybean bids found")
        return locations


class _Board:
    """The contract table, queried by (grain, price, change)."""

    def __init__(self, rows: list[tuple[str, float, float, str]]):
        self._rows = rows

    def match(self, grain: str, futures: float | None, change: float | None) -> str | None:
        if futures is None or change is None:
            return None
        hits = {
            symbol
            for row_grain, last, row_change, symbol in self._rows
            if row_grain == grain
            and abs(last - futures) <= FUTURES_TOLERANCE
            and abs(row_change - change) < 1e-9
        }
        # Two contracts can share a price, and two can share a change; a row
        # that still matches both is not worth guessing at.
        return hits.pop() if len(hits) == 1 else None


def _read_board(soup) -> _Board:
    """Parse table_2 into (grain, last, change, symbol) rows.

    Columns are read by header name - the table also carries wheat, cattle and
    energy contracts, and a bare index would silently shift if they add one.
    """
    rows: list[tuple[str, float, float, str]] = []
    header = _header(soup, BOARD_TABLE)
    if not header:
        return _Board(rows)
    try:
        i_desc = header.index("description")
        i_symbol = header.index("tickerSymbol")
        i_last = header.index("Last")
        i_change = header.index("Change")
    except ValueError:
        return _Board(rows)

    for cells in _rows(soup, BOARD_TABLE):
        if len(cells) <= max(i_desc, i_symbol, i_last, i_change):
            continue
        # "CORN December 2026" - the commodity is the first word.
        grain = normalize.classify_grain(cells[i_desc].split(" ")[0])
        if grain is None:
            continue
        last = normalize.parse_money(cells[i_last])
        change = normalize.parse_money(cells[i_change])
        if last is None or change is None:
            continue
        symbol = cells[i_symbol].lstrip("@").strip().upper()
        if symbol:
            rows.append((grain, last, round(change / 100.0, 6), symbol))
    return _Board(rows)


def _header(soup, table_id: str) -> list[str]:
    table = soup.find("table", id=table_id)
    if table is None:
        return []
    return [c.get_text(" ", strip=True) for c in table.find_all("th")]


def _rows(soup, table_id: str) -> list[list[str]]:
    table = soup.find("table", id=table_id)
    if table is None:
        return []
    return [
        [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        for tr in table.find_all("tr")
        if tr.find("td")
    ]


def _delivery_window(period: str) -> tuple[str | None, str | None]:
    """Turn a free-text delivery period into ISO start and end dates.

    ``"Dec26"``, ``"Oct 2026."`` and ``"April 27"`` are whole months.
    ``"FH Sep"`` is the 1st to the 15th and ``"LH Aug"`` the 16th to month end;
    neither carries a year, so the next occurrence of that month is used.
    """
    if not period:
        return None, None

    half = None
    m = _HALF_RE.match(period)
    if m:
        half, period = m.group(1).upper(), m.group(2)

    m = _PERIOD_RE.match(period)
    if not m:
        return None, None
    try:
        month = _dt.datetime.strptime(m.group(1)[:3].title(), "%b").month
    except ValueError:
        return None, None

    if m.group(2):
        year = int(m.group(2))
        year += 2000 if year < 100 else 0
    else:
        today = _dt.date.today()
        year = today.year + (1 if month < today.month else 0)

    last_day = _calendar.monthrange(year, month)[1]
    if half == "FH":
        return _dt.date(year, month, 1).isoformat(), _dt.date(year, month, 15).isoformat()
    if half == "LH":
        return (
            _dt.date(year, month, 16).isoformat(),
            _dt.date(year, month, last_day).isoformat(),
        )
    return (
        _dt.date(year, month, 1).isoformat(),
        _dt.date(year, month, last_day).isoformat(),
    )


def _now() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
