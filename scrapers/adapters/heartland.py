"""Adapter for Heartland Co-op's cash-bid page.

**Two formats, page first.** ``bids.htm`` is a server-rendered page: four
tables (regular and processor, corn and soybeans) whose ``<th>`` headers carry
the delivery window in ``data-start``/``data-end`` plus the futures month, and
whose cells hold two spans, cash then basis. For a few days around 2026-09-02
Heartland swapped it for an Excel "Save as Web Page" export in a frameset, and
this adapter was rewritten to read that export at ``bids_files/sheet001.htm``.
They then went back to the page - but left the export on the server, frozen at
its 9/2/2026 close. The adapter kept reading it and reporting success, so 53
pins showed September bids for six weeks. The page is now read first and the
export is only a fallback, used when the page has no bid tables.

The export holds four blocks, each the same shape::

    HEARTLAND CO-OP  ...  CORN | SOYBEANS | DIRECT CORN | DIRECT SOYBEANS
    <board rows: symbols, Prev close, High, Low, Close, Change>
    (blank)     Location   09/01/26 - 09/30 | 10/01/26 - 11/30 | ...
    CORN BIDS / Bean Bids  September-26     | Oct/Nov-26       | ...
    (blank)                CZ26             | CZ26             | ...
    Council Bluffs   67    5.06 | -0.37     | 5.10 | -0.33     | ...
    ...
    Average                4.95 | -0.48     | ...

so each location row is ``name, id, (cash, basis), (cash, basis), ...`` and the
three rows above the data carry the delivery window, its label, and the futures
month.

**Combined towns are split back out.** The new sheet quotes one row for
"Minburn/Dallas Center", "Slater/Cambridge", "Jewell/Randall" and so on, where
the old page listed each town separately. Twenty of the fifty-two mapped pins
are for those individual towns, so each half is emitted as its own location
carrying the same bids - which is what the co-op is actually quoting.

**The DIRECT blocks are delivered bids** to other companies' plants - ADM
Cedar Rapids, Cargill Blair, SIRE, Ingredion. They are Heartland's bids for
grain hauled there, not those plants' own posted bids, and company scoping in
match_locations.py keeps them off those companies' pins.
"""

from __future__ import annotations

import datetime as _dt
import re

from bs4 import BeautifulSoup

import fetch
import normalize
from adapters.base import Bid, SourceLocation

PAGE_URL = "https://myaccount.heartlandcoop.com/bids.htm"
SHEET_URL = "https://myaccount.heartlandcoop.com/bids_files/sheet001.htm"

# "CU26" / "SX26" - grain letter, month code, two-digit year.
_FUTURES_RE = re.compile(r"([A-Z]{1,2}[FGHJKMNQUVXZ]\d{2})")

# Rows that end a block or are summaries rather than delivery points.
SKIP_NAMES = {"average", "ave", "location"}

# Names the sheet now spells differently from the ids already in
# location_map.json. Each location is emitted under both.
ALIASES = {
    "MISSOURI VALLEY": ("MO VALLEY",),
}

# "09/01/26 - 09/30/26". The trailing year is optional because the sheet has
# carried it both ways; when it is absent the start's year is used, rolling
# forward if the window crosses December.
WINDOW_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*-\s*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*$")


class HeartlandAdapter:
    name = "heartland"

    def fetch(self) -> list[SourceLocation]:
        try:
            return self.parse_page(fetch.get(PAGE_URL, browser_ua=True))
        except ValueError:
            return self.parse_sheet(fetch.get(SHEET_URL, browser_ua=True))

    @classmethod
    def parse(cls, html: str) -> list[SourceLocation]:
        """Whichever format ``html`` is in."""
        try:
            return cls.parse_page(html)
        except ValueError:
            return cls.parse_sheet(html)

    # -- the server-rendered page --------------------------------------------

    @classmethod
    def parse_page(cls, html: str) -> list[SourceLocation]:
        soup = BeautifulSoup(html, "html.parser")
        as_of = cls._page_date(soup)

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
            columns = [cls._page_column(th) for th in headers[1:]]

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
                    location = collected[key] = SourceLocation(
                        source_location_id=("processor:" if is_processor else "") + name.upper(),
                        name=name,
                        state="IA",
                        as_of=as_of,
                    )
                for column, cell in zip(columns, cells[1:]):
                    bid = cls._page_cell(cell, column, grain)
                    if bid is not None:
                        location.bids.append(bid)

        locations = [loc for loc in collected.values() if loc.bids]
        if not locations:
            raise ValueError("heartland: no corn or soybean bids found on the page")
        for loc in locations:
            loc.bids.sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
        return locations

    @staticmethod
    def _page_column(th) -> dict:
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
    def _page_cell(cell, column: dict, grain: str) -> Bid | None:
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
    def _page_date(soup) -> str:
        """Read the stamp out of the "CLOSING GRAIN BIDS 101426" heading.

        The digits run together, so a 5-digit stamp is M-DD-YY and a 6-digit
        one MM-DD-YY. Anything unexpected falls back to the fetch time.
        """
        header = soup.select_one("div.header")
        if header:
            digits = re.search(r"\b(\d{5,6})\b", header.get_text(" ", strip=True))
            if digits:
                try:
                    date = _dt.datetime.strptime(digits.group(1).zfill(6), "%m%d%y").date()
                    # Heartland states these are the 1:15 PM CT close.
                    return f"{date.isoformat()}T18:15:00Z"
                except ValueError:
                    pass
        return (_dt.datetime.now(_dt.timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"))

    # -- the Excel export (fallback) -----------------------------------------

    @classmethod
    def parse_sheet(cls, html: str) -> list[SourceLocation]:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if table is None:
            raise ValueError("heartland: no table on the bid sheet")

        rows = [cls._cells(tr) for tr in table.find_all("tr")]
        as_of = cls._sheet_date(rows)

        # name -> {"id": str, "bids": [Bid]}
        collected: dict[str, dict] = {}
        grain = None

        for index, cells in enumerate(rows):
            first = cells[0].strip().lower() if cells else ""

            # A block title tells us which grain the rows below are for.
            joined = " ".join(cells).upper()
            if "HEARTLAND CO-OP" in joined:
                if "SOYBEAN" in joined:
                    grain = normalize.SOYBEANS
                elif "CORN" in joined:
                    grain = normalize.CORN
                continue

            if first not in ("corn bids", "bean bids"):
                continue
            if grain is None:
                grain = normalize.CORN if first == "corn bids" else normalize.SOYBEANS

            windows = rows[index - 1] if index else []
            symbols = rows[index + 1] if index + 1 < len(rows) else []
            columns = cls._columns(cells, windows, symbols)
            if not columns:
                continue

            for data in rows[index + 2:]:
                name = data[0].strip() if data else ""
                if not name:
                    break                       # blank row ends the block
                if name.lower().startswith(tuple(SKIP_NAMES)):
                    break                       # "Average" / "AVE (exclude...)"
                bids = cls._row_bids(data, columns, grain)
                if not bids:
                    continue
                for town in cls._split_towns(name):
                    entry = collected.setdefault(
                        town, {"id": data[1].strip() if len(data) > 1 else "",
                               "bids": []})
                    entry["bids"].extend(bids)

        locations = []
        for town, entry in collected.items():
            entry["bids"].sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
            locations.append(SourceLocation(
                # The uppercased town name, matching the ids already committed
                # in location_map.json. The sheet's numeric id would be a
                # better key but changing it would unmap every pin.
                source_location_id=town,
                name=town,
                bids=entry["bids"],
                as_of=as_of,
            ))

        if not locations:
            raise ValueError("heartland: no corn or soybean bids found on the sheet")
        return locations

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _cells(tr) -> list[str]:
        out = []
        for cell in tr.find_all(["td", "th"]):
            text = cell.get_text(" ", strip=True).replace("\xa0", " ")
            out.append(re.sub(r"\s+", " ", text).strip())
        return out

    @staticmethod
    def _split_towns(name: str) -> list[str]:
        """"Minburn/Dallas Center" -> MINBURN, DALLAS CENTER.

        The parenthetical on "Stanhope (Feed Mill)" is dropped: the old page
        listed it as STANHOPE and that is the id the pin is mapped to.

        ALIASES cover a town the sheet renamed. Spelling out "Missouri Valley"
        where the old page said "MO VALLEY" would otherwise silently unmap
        that pin - it is emitted under both names so either matches.
        """
        base = re.sub(r"\s*\([^)]*\)", "", name).strip()
        parts = [p.strip().upper() for p in base.split("/") if p.strip()]
        out = parts or [base.upper()]
        for town in list(out):
            for alias in ALIASES.get(town, ()):
                if alias not in out:
                    out.append(alias)
        return out

    @staticmethod
    def _sheet_date(rows: list[list[str]]) -> str:
        for cells in rows[:12]:
            for text in cells:
                iso = normalize.parse_date(text)
                if iso:
                    return iso + "T00:00:00Z"
        return (_dt.datetime.now(_dt.timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"))

    @classmethod
    def _columns(cls, labels: list[str], windows: list[str],
                 symbols: list[str]) -> list[dict]:
        """One entry per quoted delivery period, with the cash column index.

        Cash and basis alternate from column 2, so the label in column i of
        the header row describes the pair at data columns (2 + 2n, 3 + 2n).
        """
        out = []
        for offset, label in enumerate(labels[2:]):
            if not label.strip():
                continue
            cash_at = 2 + offset * 2
            window = windows[2 + offset] if 2 + offset < len(windows) else ""
            start, end = cls._window(window)
            out.append({
                "cash_at": cash_at,
                "label": label.strip(),
                "start": start,
                "end": end,
                "symbol": (symbols[2 + offset].strip()
                           if 2 + offset < len(symbols) else ""),
            })
        return out

    @staticmethod
    def _window(text: str) -> tuple[str | None, str | None]:
        """"09/01/26 - 09/30" -> ISO start and end.

        The end carries no year; it takes the start's, rolling forward when the
        window crosses December.
        """
        m = WINDOW_RE.match(text or "")
        if not m:
            return None, None
        mo1, d1, yy1, mo2, d2, yy2 = m.groups()

        def full_year(value: str) -> int:
            n = int(value)
            return n if n > 100 else 2000 + n

        year = full_year(yy1)
        end_year = full_year(yy2) if yy2 else year + (1 if int(mo2) < int(mo1) else 0)
        try:
            start = _dt.date(year, int(mo1), int(d1))
            end = _dt.date(end_year, int(mo2), int(d2))
        except ValueError:
            return None, None
        return start.isoformat(), end.isoformat()

    @staticmethod
    def _row_bids(data: list[str], columns: list[dict], grain: str) -> list[Bid]:
        out = []
        for col in columns:
            cash_at = col["cash_at"]
            if cash_at + 1 >= len(data):
                continue
            cash = normalize.parse_money(data[cash_at])
            if cash is None or cash <= 0:
                continue                        # this location does not quote it
            basis = normalize.parse_money(data[cash_at + 1])
            out.append(Bid(
                grain=grain,
                delivery_start=col["start"],
                delivery_end=col["end"],
                delivery_label=(normalize.format_delivery_label(col["start"], col["end"])
                                or col["label"]),
                futures_month=normalize.futures_month_from_symbol(col["symbol"]),
                futures=round(cash - basis, 4) if basis is not None else None,
                futures_change=None,
                basis=basis,
                cash=cash,
            ))
        return out
