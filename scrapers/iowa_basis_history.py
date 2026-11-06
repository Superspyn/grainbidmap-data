"""Import the Iowa Department of Agriculture's daily basis history.

The department's Agricultural Diversification & Market Development Bureau
publishes month-end grain reports - one PDF per year back to 1974, one per
month for the current year - at
https://iowaagriculture.gov/agricultural-diversification-market-development-bureau/grain-reports

From 2006 at the latest each month carries two tables per grain:

    IOWA DAILY CORN BASIS FOR NOVEMBER 2006
    NW - I NC - II NE - III SW - IV SC - V SE - VI Average
    1 -0.35 -0.35 -0.36 -0.37 -0.35 -0.31 -0.35
    ...
    Basis determined by using December contract.

and the matching "STATE AVERAGE CORN PRICES" cash table. Both are read into
``iowa_district`` in the bid-history database: one row per day, district and
grain. This is the long seasonal record our own scraped history (since
2026-08-26) does not have yet. North Central (NC) is the farm's district.

Years before basis tables were published contribute cash prices only.

robots.txt allows /sites/default/files/. The PDFs are cached under
``~/grain-bid-history/iowa-ag-reports`` and a past year is never downloaded
twice; only the current year's monthly files are re-checked.

    python scrapers/iowa_basis_history.py                # fetch what is new, import all
    python scrapers/iowa_basis_history.py --if-stale-days 7   # what the bid task runs
    python scrapers/iowa_basis_history.py --summary
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import pathlib
import re
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bid_history  # noqa: E402
import fetch  # noqa: E402

INDEX = ("https://iowaagriculture.gov/agricultural-diversification-market-"
         "development-bureau/grain-reports")
SITE = "https://iowaagriculture.gov"
CACHE = bid_history.DB.parent / "iowa-ag-reports"
STAMP = CACHE / ".last-run"
FIRST_YEAR = 1995          # nothing earlier is read; earlier years have no basis
DISTRICTS = ("NW", "NC", "NE", "SW", "SC", "SE", "STATE")
MAX_BASIS = 2.50

SCHEMA = """
CREATE TABLE IF NOT EXISTS iowa_district (
    day      TEXT NOT NULL,
    district TEXT NOT NULL,   -- NW NC NE SW SC SE, or STATE for the average
    grain    TEXT NOT NULL,   -- corn | soybeans
    cash     REAL,
    basis    REAL,
    contract TEXT,            -- futures month the basis is against, e.g. DEC
    PRIMARY KEY (day, district, grain)
);
"""

MONTHS = {m.upper(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
TITLE_RE = re.compile(
    r"(?:DAILY\s+(CORN|SOYBEAN|BEAN)\s+BASIS|STATE\s+AVERAGE\s+(CORN|SOYBEAN|BEAN)\s+PRICES)"
    r"\s+FOR\s+([A-Z]+)\s+(\d{4})", re.I)
ROW_RE = re.compile(r"^\s*(\d{1,2})\s+((?:-?\d*\.\d+\s+){6}-?\d*\.\d+)\s*$")
CONTRACT_RE = re.compile(r"using\s+\(?\s*([A-Za-z]+)\s*\)?\s+contract", re.I)


def parse_page(text: str, file_year: int | None = None) -> list[tuple]:
    """Rows of (day, district, grain, kind, value, contract) from one page.

    ``file_year`` is the year of the report the page came from. A title year
    more than one off it is a typo - the 2018 report has a "January 2918" -
    and the report's own year is used instead.
    """
    m = TITLE_RE.search(text or "")
    if not m:
        return []
    kind = "basis" if m.group(1) else "cash"
    grain = "corn" if (m.group(1) or m.group(2)).upper() == "CORN" else "soybeans"
    month = MONTHS.get(m.group(3).upper())
    year = int(m.group(4))
    if file_year and abs(year - file_year) > 1:
        year = file_year
    if not month:
        return []
    c = CONTRACT_RE.search(text)
    contract = c.group(1).upper()[:3] if c and kind == "basis" else None
    out = []
    for line in text.splitlines():
        r = ROW_RE.match(line)
        if not r:
            continue
        try:
            day = _dt.date(year, month, int(r.group(1))).isoformat()
        except ValueError:
            continue
        values = [float(v) for v in r.group(2).split()]
        for district, value in zip(DISTRICTS, values):
            out.append((day, district, grain, kind, value, contract))
    return out


def report_links() -> list[str]:
    page = fetch.get(INDEX)
    links = {html.unescape(h) for h in re.findall(r'href="([^"]+\.pdf)"', page, re.I)}
    return sorted(urllib.parse.urljoin(SITE, h) for h in links
                  if "Monthly%20grain%20reports" in h or "Monthly grain reports" in h)


def _year_of(url: str) -> int | None:
    years = [int(y) for y in re.findall(r"(?<!\d)(19\d\d|20\d\d)(?!\d)", urllib.parse.unquote(url))]
    return max(years) if years else None


def download(links: list[str]) -> list[pathlib.Path]:
    import requests
    CACHE.mkdir(parents=True, exist_ok=True)
    this_year = _dt.date.today().year
    files = []
    for url in links:
        year = _year_of(url)
        if year is None or year < FIRST_YEAR:
            continue
        name = urllib.parse.unquote(url.split("/")[-1])
        local = CACHE / f"{year}" / name
        # A past year's file does not change; this year's monthly files are
        # fetched once each, as they appear.
        if not local.exists():
            local.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(url, headers={"User-Agent": fetch.USER_AGENT}, timeout=60)
            r.raise_for_status()
            local.write_bytes(r.content)
            print(f"  downloaded {year}/{name} ({len(r.content) // 1024} KB)", file=sys.stderr)
            time.sleep(2)        # be gentle with a state agency's server
        files.append(local)
    return files


def import_files(db, files: list[pathlib.Path]) -> int:
    import pypdf
    db.executescript(SCHEMA)
    merged: dict[tuple, dict] = {}
    for path in files:
        file_year = int(path.parent.name) if path.parent.name.isdigit() else None
        try:
            for page in pypdf.PdfReader(path).pages:
                text = page.extract_text() or ""
                for day, district, grain, kind, value, contract in parse_page(text, file_year):
                    row = merged.setdefault((day, district, grain), {})
                    row[kind] = value
                    if contract:
                        row["contract"] = contract
        except Exception as e:                      # one bad PDF must not stop the rest
            print(f"  skipped {path.name}: {e}", file=sys.stderr)
    # The source has the odd typo: a price keyed into the basis table
    # (March 2023 corn "basis" of 6.98), a 0.00 cash price (SC, 2008-12-05)
    # beside a -3.09 basis. No Iowa basis has been beyond +/-$2.50 against the
    # nearby contract, so anything past that is blanked, as is a zero price
    # and the basis printed next to it. Day-to-day noise inside that range is
    # left for the forecast to smooth - it is the source's own record.
    for row in merged.values():
        if row.get("cash") is not None and row["cash"] <= 0:
            row["cash"] = None
            row["basis"] = None
        if row.get("basis") is not None and abs(row["basis"]) > MAX_BASIS:
            row["basis"] = None
    with db:
        # Every cached report is read on every run, so the table is rebuilt
        # whole rather than merged into - a correction in the source, or in
        # the cleaning above, then takes effect.
        db.execute("DELETE FROM iowa_district")
        db.executemany(
            "INSERT INTO iowa_district VALUES (?, ?, ?, ?, ?, ?)",
            [(d, dist, g, r.get("cash"), r.get("basis"), r.get("contract"))
             for (d, dist, g), r in merged.items()])
    return len(merged)


def summary(db) -> None:
    db.executescript(SCHEMA)
    for grain in ("corn", "soybeans"):
        n, first, last = db.execute(
            "SELECT COUNT(*), MIN(day), MAX(day) FROM iowa_district "
            "WHERE district = 'NC' AND grain = ? AND basis IS NOT NULL", (grain,)).fetchone()
        print(f"North Central {grain} basis: {n} days ({first} to {last})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--if-stale-days", type=float, default=None,
                    help="do nothing if the last successful run was more recent than this")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    db = bid_history.connect()
    if args.summary:
        summary(db)
        return 0
    if args.if_stale_days is not None and STAMP.exists():
        age = time.time() - STAMP.stat().st_mtime
        if age < args.if_stale_days * 86400:
            return 0
    files = download(report_links())
    print(f"iowa basis: {import_files(db, files)} district-days from {len(files)} reports",
          file=sys.stderr)
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.touch()
    summary(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
