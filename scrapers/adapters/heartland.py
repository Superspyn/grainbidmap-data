"""Adapter for Heartland Co-op's cash-bid sheet.

Heartland replaced their server-rendered bids page with an Excel "Save as Web
Page" export in a frameset. ``bids.htm`` is now only the frameset - the data
lives in ``bids_files/sheet001.htm`` - and the old landmarks (CORN BIDS
headings in ``<th>``, ``basis-num`` cells) are gone entirely, so the previous
parser found nothing and the source failed rather than returning wrong numbers.

The sheet holds four blocks, each the same shape::

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

URL = "https://myaccount.heartlandcoop.com/bids_files/sheet001.htm"

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
        return self.parse(fetch.get(URL, browser_ua=True))

    @classmethod
    def parse(cls, html: str) -> list[SourceLocation]:
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
