#!/usr/bin/env python3
"""Report which co-ops refreshed and which failed, from the published feed.

``docs/bids.json`` already records per-source health - ok, error, stale,
fetched_at, counts - but nothing surfaced it, so a source could quietly fail
for days behind a run that still says "pushed".

    python scrapers/source_status.py           # readable table
    python scrapers/source_status.py --log     # one line per problem, or silence
    python scrapers/source_status.py --strict  # exit 1 if anything is failing

``--log`` is what update-bids.ps1 calls: it prints nothing when every source is
healthy, so a clean run stays quiet in the log and a failure stands out.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
FEED = REPO / "docs" / "bids.json"

# Older than this and a source is worth mentioning even if it claims ok.
STALE_HOURS = 36


def _age_hours(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        when = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = _dt.datetime.now(_dt.timezone.utc)
    return (now - when).total_seconds() / 3600.0


def _age_text(hours: float | None) -> str:
    if hours is None:
        return "never"
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    if hours < 48:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"


def collect(feed_path: pathlib.Path = FEED) -> list[dict]:
    data = json.loads(feed_path.read_text(encoding="utf-8"))
    pins_by_source: dict[str, int] = {}
    for loc in data.get("locations", {}).values():
        pins_by_source[loc.get("source")] = pins_by_source.get(loc.get("source"), 0) + 1

    rows = []
    for source_id, info in sorted(data.get("sources", {}).items()):
        hours = _age_hours(info.get("fetched_at"))
        if not info.get("ok"):
            state = "FAILED"
        elif hours is not None and hours > STALE_HOURS:
            state = "STALE"
        else:
            state = "ok"
        rows.append({
            "id": source_id,
            "label": info.get("label") or source_id,
            "state": state,
            "pins": pins_by_source.get(source_id, 0),
            "locations": info.get("locations"),
            "bids": info.get("bids"),
            "fetched_at": info.get("fetched_at"),
            "age_hours": hours,
            "error": info.get("error"),
        })
    rows.sort(key=lambda r: ({"FAILED": 0, "STALE": 1, "ok": 2}[r["state"]], -r["pins"]))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", action="store_true",
                    help="print only problems; silent when all sources are healthy")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any source is failing or stale")
    ap.add_argument("--feed", type=pathlib.Path, default=FEED)
    args = ap.parse_args()

    if not args.feed.exists():
        print(f"no feed at {args.feed}", file=sys.stderr)
        return 1

    rows = collect(args.feed)
    bad = [r for r in rows if r["state"] != "ok"]

    if args.log:
        for r in bad:
            why = r["error"] or f"last refreshed {_age_text(r['age_hours'])}"
            print(f"{r['state']}: {r['label']} ({r['pins']} pins) - {why[:120]}")
    else:
        print(f"{'SOURCE':<26}{'STATE':<8}{'PINS':>5}{'BIDS':>7}  {'LAST OK':<12} NOTE")
        for r in rows:
            note = (r["error"] or "")[:60]
            print(f"{r['label'][:25]:<26}{r['state']:<8}{r['pins']:>5}"
                  f"{r['bids'] or 0:>7}  {_age_text(r['age_hours']):<12} {note}")
        print()
        print(f"{len(rows) - len(bad)} of {len(rows)} sources healthy")

    return 1 if (args.strict and bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())
