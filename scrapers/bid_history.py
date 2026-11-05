"""Daily bid history: one closing quote per pin, grain and delivery window.

Every run of the bid task overwrites today's row for each quote, so what is
left at the end of the day is that day's close - the series basis forecasting
needs. The day is the quote's own date (its ``as_of``, in Central time), not
the day it was scraped: Heartland posts the previous day's close, and an
elevator that does not update over a weekend keeps Friday's date instead of
gaining copies for Saturday and Sunday.

Carried-forward ("stale") pins are skipped - those are old quotes re-shown
while a source is down, not new prices.

The database lives outside the repo, at ``~/grain-bid-history/bid-history.sqlite``.
It holds nothing private (these are the public bids the map shows), and it can
always be rebuilt: every published bids.json is in this repo's git history, and
``--backfill`` replays them.

    python scrapers/bid_history.py               # record docs/bids.json now
    python scrapers/bid_history.py --backfill    # replay every committed bids.json
    python scrapers/bid_history.py --summary     # what is stored
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import sqlite3
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BIDS = REPO / "docs" / "bids.json"
DB = pathlib.Path.home() / "grain-bid-history" / "bid-history.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    day            TEXT NOT NULL,   -- quote date, Central time, YYYY-MM-DD
    pin            TEXT NOT NULL,
    grain          TEXT NOT NULL,
    delivery_start TEXT NOT NULL DEFAULT '',
    delivery_end   TEXT NOT NULL DEFAULT '',
    delivery_label TEXT NOT NULL DEFAULT '',
    futures_month  TEXT,
    futures        REAL,
    basis          REAL,
    cash           REAL,
    as_of          TEXT,
    captured_at    TEXT NOT NULL,   -- when the scrape that recorded it ran (UTC)
    PRIMARY KEY (day, pin, grain, delivery_start, delivery_end, delivery_label)
);
CREATE INDEX IF NOT EXISTS quotes_pin ON quotes (pin, grain, day);
CREATE TABLE IF NOT EXISTS pins (
    pin    TEXT PRIMARY KEY,
    name   TEXT,
    source TEXT
);
"""


def connect(path: pathlib.Path = DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def _parse(ts: str | None) -> _dt.datetime | None:
    if not ts:
        return None
    try:
        when = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=_dt.timezone.utc)


def central_day(when: _dt.datetime) -> str:
    """UTC-6h: Central standard time. A whole-day bucket, so the hour DST
    moves only matters for a quote stamped between 11 PM and midnight."""
    return (when.astimezone(_dt.timezone.utc) - _dt.timedelta(hours=6)).date().isoformat()


def record(db: sqlite3.Connection, data: dict) -> int:
    """Upsert every fresh quote in one built bids.json. Returns rows written."""
    captured = _parse(data.get("generated_at"))
    if captured is None:
        return 0
    captured_at = captured.isoformat(timespec="seconds").replace("+00:00", "Z")
    rows, pins = [], []
    for pin, loc in (data.get("locations") or {}).items():
        if loc.get("stale"):
            continue
        quoted = _parse(loc.get("as_of")) or captured
        if quoted > captured:          # a source stamping the future: trust the scrape
            quoted = captured
        day = central_day(quoted)
        pins.append((pin, loc.get("name"), loc.get("source")))
        for b in loc.get("bids") or []:
            if b.get("cash") is None or b.get("grain") not in ("corn", "soybeans"):
                continue
            rows.append((day, pin, b["grain"],
                         b.get("delivery_start") or "", b.get("delivery_end") or "",
                         b.get("delivery_label") or "", b.get("futures_month"),
                         b.get("futures"), b.get("basis"), b.get("cash"),
                         loc.get("as_of"), captured_at))
    with db:
        db.executemany(
            "INSERT INTO pins (pin, name, source) VALUES (?, ?, ?) "
            "ON CONFLICT(pin) DO UPDATE SET name = excluded.name, source = excluded.source",
            pins)
        # A later scrape replaces an earlier one for the same day, never the
        # reverse - the backfill and the live task can then run in any order.
        db.executemany(
            "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(day, pin, grain, delivery_start, delivery_end, delivery_label) "
            "DO UPDATE SET futures_month = excluded.futures_month, futures = excluded.futures, "
            "basis = excluded.basis, cash = excluded.cash, as_of = excluded.as_of, "
            "captured_at = excluded.captured_at "
            "WHERE excluded.captured_at >= quotes.captured_at",
            rows)
    return len(rows)


def backfill(db: sqlite3.Connection) -> None:
    shas = subprocess.run(
        ["git", "-C", str(REPO), "log", "--reverse", "--format=%H", "--", "docs/bids.json"],
        capture_output=True, text=True, check=True).stdout.split()
    print(f"replaying {len(shas)} committed bids.json files", file=sys.stderr)
    for n, sha in enumerate(shas, 1):
        blob = subprocess.run(["git", "-C", str(REPO), "show", f"{sha}:docs/bids.json"],
                              capture_output=True, check=True).stdout
        try:
            record(db, json.loads(blob))
        except (ValueError, KeyError, TypeError) as e:
            print(f"  skipped {sha[:7]}: {e}", file=sys.stderr)
        if n % 100 == 0:
            print(f"  {n}/{len(shas)}", file=sys.stderr)


def summary(db: sqlite3.Connection) -> None:
    n, days, first, last = db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT day), MIN(day), MAX(day) FROM quotes").fetchone()
    pins = db.execute("SELECT COUNT(DISTINCT pin) FROM quotes").fetchone()[0]
    print(f"{n} daily quotes, {pins} pins, {days} days ({first} to {last})")
    print(f"database: {DB}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    db = connect()
    if args.backfill:
        backfill(db)
    elif not args.summary:
        print(f"bid history: recorded {record(db, json.loads(BIDS.read_text(encoding='utf-8')))} quotes")
    if args.backfill or args.summary:
        summary(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
