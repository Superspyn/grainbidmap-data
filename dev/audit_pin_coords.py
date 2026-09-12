"""Check the map's pin coordinates against the co-ops' own published ones.

    python dev/audit_pin_coords.py            # audit, write a report
    python dev/audit_pin_coords.py --refresh  # re-fetch the sources first

Why this exists: fifteen Five Star Cooperative pins were added from state
permit records and Google results, and fourteen of them were wrong - one by
4.3 miles. The same pipeline produced most of the other 880 pins, so the
question "how many of the rest are wrong" is open and matters: a pin in the
wrong place gives the wrong haul distance, and therefore the wrong net
price, which is the entire point of the map.

Several bid feeds publish latitude and longitude for each of their own
delivery points. That is the co-op saying where its own elevator is, which
beats any third-party geocode, and it costs one request per source because
each feed returns every location at once. This joins those coordinates to
the pins through scrapers/config/location_map.json and reports the gaps.

It changes nothing. It writes a report; moving a pin stays a decision made
with the aerial imagery in front of you.
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scrapers"))

import build_bids  # noqa: E402
import match_locations  # noqa: E402

CACHE = ROOT / "dev" / "source-coords.json"
REPORT = ROOT / "dev" / "pin-coord-audit.csv"

# Distances beyond which a pin is worth a look. A large elevator complex can
# be a few hundred metres across and the feed's point may be the office while
# the pin is the scale, so small gaps are not evidence of anything.
LOOK_KM = 1.0
WRONG_KM = 3.0


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. The pins span Iowa to Missouri, so the flat
    approximation is fine, but this is cheap and has no error to explain."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def fetch_source_coords() -> dict:
    """Every source location that publishes a coordinate, by source id."""
    specs = build_bids.load_sources(None)
    out: dict[str, dict] = {}
    for spec in specs:
        source_id = spec["id"]
        try:
            locations = build_bids.build_adapter(spec).fetch()
        except Exception as exc:  # noqa: BLE001
            print(f"  {source_id}: FAILED {type(exc).__name__}: {exc}")
            continue
        have = {}
        for loc in locations:
            if loc.latitude is None or loc.longitude is None:
                continue
            have[str(loc.source_location_id)] = {"name": loc.name,
                                                 "lat": float(loc.latitude),
                                                 "lon": float(loc.longitude)}
        if have:
            out[source_id] = have
        print(f"  {source_id}: {len(have)} of {len(locations)} locations "
              f"publish a coordinate")
    return out


def main() -> None:
    if "--refresh" in sys.argv or not CACHE.exists():
        print("Fetching source coordinates (one request per source)...")
        coords = fetch_source_coords()
        CACHE.write_text(json.dumps(coords, indent=1), encoding="utf-8")
        print(f"wrote {CACHE}")
    else:
        coords = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"using cached coordinates from {CACHE} (--refresh to re-fetch)")

    pins = {p["id"]: p for p in match_locations.load_pins(match_locations.find_html())}
    mapping = json.loads(
        (ROOT / "scrapers" / "config" / "location_map.json").read_text(encoding="utf-8"))
    entries = mapping["pins"]

    rows = []
    checked = unmatched = no_coord = 0
    for pin_id, entry in entries.items():
        pin = pins.get(pin_id)
        if not pin:
            unmatched += 1
            continue
        source = entry.get("source")
        loc_id = str(entry.get("source_location_id"))
        published = (coords.get(source) or {}).get(loc_id)
        if not published:
            no_coord += 1
            continue
        checked += 1
        gap = km(pin["lat"], pin["lng"], published["lat"], published["lon"])
        rows.append({
            "pin_id": pin_id,
            "pin_name": pin["name"],
            "source": source,
            "source_name": published["name"],
            "km": round(gap, 3),
            "pin_lat": pin["lat"], "pin_lng": pin["lng"],
            "src_lat": published["lat"], "src_lon": published["lon"],
        })

    rows.sort(key=lambda r: -r["km"])
    import csv
    with REPORT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["pin_id"])
        writer.writeheader()
        writer.writerows(rows)

    worst = [r for r in rows if r["km"] >= WRONG_KM]
    look = [r for r in rows if LOOK_KM <= r["km"] < WRONG_KM]
    print(f"\n{len(pins)} pins on the map")
    print(f"  {checked} could be checked against the co-op's own coordinate")
    print(f"  {no_coord} matched a source that publishes no coordinate")
    print(f"  {unmatched} mapped to a pin id that is no longer on the map")
    print(f"\n  {len(worst)} more than {WRONG_KM} km out")
    print(f"  {len(look)} between {LOOK_KM} and {WRONG_KM} km out")
    if rows:
        mid = rows[len(rows) // 2]["km"]
        print(f"  median gap {mid:.3f} km")
    print(f"\nwrote {REPORT}")
    for row in worst[:25]:
        print(f"  {row['km']:8.2f} km  {row['pin_name'][:34]:36s} "
              f"vs {row['source_name'][:28]:30s} [{row['source']}]")


if __name__ == "__main__":
    main()
