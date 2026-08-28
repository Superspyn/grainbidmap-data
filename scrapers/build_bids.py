#!/usr/bin/env python3
"""Build docs/bids.json from every configured cash-bid source.

Run modes
---------
  python scrapers/build_bids.py                      # fetch everything, write docs/bids.json
  python scrapers/build_bids.py --source heartland   # limit to one source
  python scrapers/build_bids.py --dry-run            # print a table, write nothing
  python scrapers/build_bids.py --raw                # dump raw source locations for matching
  python scrapers/build_bids.py --validate FILE      # sanity-check a built file

Failure isolation is the important behaviour here: a source that fails must
never blank the published file. Whatever that source contributed last time is
carried forward and flagged ``stale``, and the process only exits non-zero if
*every* source failed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import yaml  # noqa: E402

from adapters.agricharts import AgriChartsAdapter  # noqa: E402
from adapters.cihedging import CIHedgingAdapter  # noqa: E402
from adapters.gradable import GradableAdapter  # noqa: E402
from adapters.heartland import HeartlandAdapter  # noqa: E402
from adapters.landus import LandusAdapter  # noqa: E402
from adapters.newcoop import NewCoopAdapter  # noqa: E402
from adapters.nexus import NexusAdapter  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFIG = REPO / "scrapers" / "config" / "sources.yaml"
LOCATION_MAP = REPO / "scrapers" / "config" / "location_map.json"
OUTPUT = REPO / "docs" / "bids.json"
RAW_OUTPUT = REPO / "scrapers" / ".build" / "raw.json"

STALE_AFTER_HOURS = 48


def now_iso() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def build_adapter(spec: dict):
    kind = spec["adapter"]
    if kind == "agricharts":
        return AgriChartsAdapter(
            spec["tenant"],
            months=spec.get("months", 12),
            referer=spec.get("referer"),
        )
    if kind == "heartland":
        return HeartlandAdapter()
    if kind == "newcoop":
        return NewCoopAdapter()
    if kind == "landus":
        return LandusAdapter()
    if kind == "nexus":
        return NexusAdapter()
    if kind == "gradable":
        return GradableAdapter(spec["tenant"])
    if kind == "cihedging":
        return CIHedgingAdapter(
            spec["company_id"], spec.get("label", ""), spec.get("referer")
        )
    raise ValueError("unknown adapter type: " + str(kind))


def load_sources(only: list[str] | None) -> list[dict]:
    specs = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["sources"]
    if only:
        specs = [s for s in specs if s["id"] in only]
        missing = set(only) - {s["id"] for s in specs}
        if missing:
            raise SystemExit("unknown source id(s): " + ", ".join(sorted(missing)))
    return specs


def scrape(specs: list[dict]) -> tuple[dict, dict]:
    """Fetch every source. Returns (results_by_source_id, status_by_source_id)."""
    results: dict[str, list] = {}
    status: dict[str, dict] = {}

    for spec in specs:
        source_id = spec["id"]
        try:
            locations = build_adapter(spec).fetch()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            print("  " + source_id + ": FAILED - " + type(exc).__name__ + ": " + str(exc),
                  file=sys.stderr)
            status[source_id] = {
                "ok": False,
                "label": spec.get("label", source_id),
                "error": (type(exc).__name__ + ": " + str(exc))[:300],
            }
            continue

        bid_count = sum(len(loc.bids) for loc in locations)
        print("  " + source_id + ": " + str(len(locations)) + " locations, "
              + str(bid_count) + " bids")
        results[source_id] = locations
        status[source_id] = {
            "ok": True,
            "label": spec.get("label", source_id),
            "fetched_at": now_iso(),
            "locations": len(locations),
            "bids": bid_count,
        }

    return results, status


def load_location_map() -> dict:
    """pinId -> {source, source_location_id}, produced by match_locations.py."""
    if not LOCATION_MAP.exists():
        return {}
    data = json.loads(LOCATION_MAP.read_text(encoding="utf-8"))
    return data.get("pins", data)


def dedupe_bids(bids) -> list[dict]:
    """Drop rows that repeat an earlier one in every field, keeping order.

    A few feeds list the same location twice and emit its table twice with it.
    Only fully identical rows are collapsed: two bids can legitimately share a
    delivery window and differ in basis, which is a real distinction the
    merchandiser is drawing, not a duplicate.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for bid in bids:
        key = tuple(sorted(bid.items(), key=lambda kv: kv[0]))
        if key in seen:
            continue
        seen.add(key)
        out.append(bid)
    return out


def assemble(results: dict, status: dict, previous: dict) -> dict:
    """Project scraped source data onto map pins, carrying forward stale sources."""
    pin_map = load_location_map()
    if not pin_map:
        print("  note: no location_map.json yet - run match_locations.py to link pins",
              file=sys.stderr)

    # Index every scraped location by (source_id, source_location_id).
    by_key: dict[tuple[str, str], object] = {}
    for source_id, locations in results.items():
        for loc in locations:
            by_key[(source_id, str(loc.source_location_id))] = loc

    previous_locations = previous.get("locations", {})
    out_locations: dict[str, dict] = {}

    for pin_id, entry in pin_map.items():
        source_id = entry.get("source")
        location = by_key.get((source_id, str(entry.get("source_location_id"))))

        if location is not None:
            out_locations[pin_id] = {
                "name": entry.get("name") or location.name,
                "source": source_id,
                "source_name": location.name,
                "as_of": location.as_of or status.get(source_id, {}).get("fetched_at"),
                "bids": dedupe_bids(b.as_dict() for b in location.bids),
            }
        elif pin_id in previous_locations:
            # This source failed (or dropped the location) - keep the last good
            # data and mark it so the front end can show it as stale.
            carried = dict(previous_locations[pin_id])
            carried["stale"] = True
            out_locations[pin_id] = carried

    # Sources that failed keep their previous status, flagged stale.
    previous_status = previous.get("sources", {})
    for source_id, info in status.items():
        if not info["ok"] and source_id in previous_status:
            merged = dict(previous_status[source_id])
            merged.update({"ok": False, "stale": True, "error": info["error"]})
            status[source_id] = merged

    return {
        "generated_at": now_iso(),
        "disclaimer": (
            "Cash bids are collected from each facility's public bid page for "
            "reference only and may be delayed or out of date. Confirm with the "
            "elevator before hauling."
        ),
        "sources": status,
        "locations": out_locations,
    }


def write_raw(results: dict) -> None:
    RAW_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now_iso(),
        "sources": {
            source_id: [
                {
                    "source_location_id": loc.source_location_id,
                    "name": loc.name,
                    "city": loc.city,
                    "state": loc.state,
                    "latitude": loc.latitude,
                    "longitude": loc.longitude,
                    "as_of": loc.as_of,
                    "bids": [b.as_dict() for b in loc.bids],
                }
                for loc in locations
            ]
            for source_id, locations in results.items()
        },
    }
    RAW_OUTPUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("wrote " + str(RAW_OUTPUT.relative_to(REPO)))


def _num(value) -> str:
    if value is None:
        return "-"
    return ("%.4f" % value).rstrip("0").rstrip(".")


def print_dry_run(results: dict) -> None:
    header = "    %-9s %-16s %-6s %9s %8s %8s %8s" % (
        "grain", "delivery", "month", "futures", "change", "basis", "cash")
    for source_id, locations in results.items():
        print("\n=== " + source_id + " - " + str(len(locations)) + " locations ===")
        for loc in locations:
            where = " (" + str(loc.city) + ", " + str(loc.state) + ")" if loc.city else ""
            print("\n  " + loc.name + where + "  [id=" + str(loc.source_location_id)
                  + "]  as_of=" + str(loc.as_of))
            print(header)
            for b in loc.bids:
                print("    %-9s %-16s %-6s %9s %8s %8s %8s" % (
                    b.grain,
                    b.delivery_label or "",
                    b.futures_month or "",
                    _num(b.futures),
                    _num(b.futures_change),
                    _num(b.basis),
                    _num(b.cash),
                ))


def validate(path: pathlib.Path) -> int:
    """Check a built file for the mistakes that would quietly break the map."""
    data = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []

    locations = data.get("locations", {})
    if not locations:
        problems.append("no locations in file")

    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=STALE_AFTER_HOURS)
    stale = 0
    for pin_id, loc in locations.items():
        bids = loc.get("bids") or []
        if not bids:
            problems.append(pin_id + ": no bids")
            continue
        for bid in bids:
            if bid.get("cash") is None:
                problems.append(pin_id + ": bid with null cash price")
            if bid.get("grain") not in ("corn", "soybeans"):
                problems.append(pin_id + ": unexpected grain " + repr(bid.get("grain")))
        as_of = loc.get("as_of")
        if as_of:
            try:
                when = _dt.datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                if when < cutoff:
                    stale += 1
            except ValueError:
                problems.append(pin_id + ": unparseable as_of " + repr(as_of))

    total_bids = sum(len(l.get("bids") or []) for l in locations.values())
    print(path.name + ": " + str(len(locations)) + " locations, " + str(total_bids)
          + " bids, " + str(stale) + " older than " + str(STALE_AFTER_HOURS) + "h")

    for source_id, info in data.get("sources", {}).items():
        flag = "ok" if info.get("ok") else "FAILED (" + str(info.get("error", "?")) + ")"
        print("  " + source_id + ": " + flag)

    if problems:
        print("\n" + str(len(problems)) + " problem(s):", file=sys.stderr)
        for problem in problems[:25]:
            print("  - " + problem, file=sys.stderr)
        return 1
    print("validation passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append",
                        help="limit to this source id (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print results, write nothing")
    parser.add_argument("--raw", action="store_true",
                        help="also write the raw source dump")
    parser.add_argument("--validate", metavar="FILE",
                        help="validate a built bids.json and exit")
    parser.add_argument("--cache", action="store_true",
                        help="use the on-disk HTTP cache")
    args = parser.parse_args()

    if args.validate:
        return validate(pathlib.Path(args.validate))

    if args.cache:
        os.environ["BIDS_CACHE"] = "1"

    specs = load_sources(args.source)
    print("fetching " + str(len(specs)) + " source(s)...")
    results, status = scrape(specs)

    if not results:
        print("every source failed", file=sys.stderr)
        return 1

    if args.dry_run:
        print_dry_run(results)
        if args.raw:
            write_raw(results)
        return 0

    if args.raw:
        write_raw(results)

    previous: dict = {}
    if OUTPUT.exists():
        try:
            previous = json.loads(OUTPUT.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("  note: existing bids.json was unreadable, rebuilding", file=sys.stderr)

    payload = assemble(results, status, previous)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    total_bids = sum(len(l["bids"]) for l in payload["locations"].values())
    print("wrote " + str(OUTPUT.relative_to(REPO)) + ": "
          + str(len(payload["locations"])) + " pins, " + str(total_bids) + " bids")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
