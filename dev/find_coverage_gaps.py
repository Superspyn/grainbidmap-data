"""Find bids we already fetch but do not show on any pin.

    python dev/find_coverage_gaps.py

Every source returns all of its delivery points in one request, and
location_map.json wires some of them to pins. Whatever is left over is a
price already arriving on this machine with nowhere to appear - the
cheapest coverage there is, because it needs no new source, no new
request, and no one else's permission.

Two kinds of leftover, reported separately because they mean different
things:

  * an unused source location that sits close to an unmatched pin - almost
    certainly a match that was missed, and worth wiring up
  * an unused source location with no pin near it at all - a delivery point
    the map simply does not have, and a candidate for a new pin

Proximity is only offered where the source publishes a coordinate. Where it
does not, the name is all there is, and a name match alone has already
produced one wrong join on this map, so those are listed for a human rather
than proposed.
"""
from __future__ import annotations

import csv
import difflib
import json
import math
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scrapers"))

import build_bids  # noqa: E402
import match_locations  # noqa: E402

MAP = ROOT / "scrapers" / "config" / "location_map.json"
REPORT = ROOT / "dev" / "coverage-gaps.csv"

NEAR_KM = 8.0          # same threshold match_locations uses for a pin match
NAME_FLOOR = 0.55      # how alike two names must read to be worth proposing


def km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def simplify(name: str) -> str:
    """Strip everything that is not the place: company, facility type, punctuation."""
    s = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    drop = {"coop", "co", "op", "cooperative", "inc", "llc", "the", "company",
            "grain", "elevator", "feed", "mill", "ethanol", "energy", "plant",
            "terminal", "seasonal", "approx", "ia", "mo", "il", "mn", "ne",
            "sd", "ks", "tx", "wi", "farmers", "ag", "llp", "site", "north",
            "south", "east", "west", "annex"}
    return " ".join(w for w in s.split() if w not in drop).strip()


def main() -> None:
    pins = match_locations.load_pins(match_locations.find_html())
    mapping = json.loads(MAP.read_text(encoding="utf-8"))["pins"]
    matched_pin_ids = set(mapping)
    used_by_source: dict[str, set] = {}
    for entry in mapping.values():
        used_by_source.setdefault(entry["source"], set()).add(
            str(entry["source_location_id"]))

    free_pins = [p for p in pins if p["id"] not in matched_pin_ids]
    print(f"{len(pins)} pins, {len(matched_pin_ids)} matched, "
          f"{len(free_pins)} without a source\n")

    rows = []
    for spec in build_bids.load_sources(None):
        source_id = spec["id"]
        try:
            locations = build_bids.build_adapter(spec).fetch()
        except Exception as exc:  # noqa: BLE001
            print(f"  {source_id}: FAILED {type(exc).__name__}")
            continue
        used = used_by_source.get(source_id, set())
        spare = [l for l in locations
                 if str(l.source_location_id) not in used and l.bids]
        if not spare:
            continue
        print(f"  {source_id}: {len(spare)} unused location(s) carrying bids")
        for loc in spare:
            best_km = best_name = None
            simple = simplify(loc.name)
            for pin in free_pins:
                by_name = difflib.SequenceMatcher(
                    None, simple, simplify(pin["name"])).ratio()
                gap = None
                if loc.latitude is not None and loc.longitude is not None:
                    gap = km(loc.latitude, loc.longitude, pin["lat"], pin["lng"])
                score = (gap is not None and gap <= NEAR_KM, by_name)
                if best_name is None or score > best_name[0]:
                    best_name = (score, pin, by_name, gap)
            pin = best_name[1] if best_name else None
            rows.append({
                "source": source_id,
                "source_location": loc.name,
                "source_location_id": loc.source_location_id,
                "bids": len(loc.bids),
                "src_lat": loc.latitude or "",
                "src_lon": loc.longitude or "",
                "nearest_free_pin": pin["name"] if pin else "",
                "pin_id": pin["id"] if pin else "",
                "name_similarity": round(best_name[2], 2) if best_name else "",
                "km_to_pin": round(best_name[3], 2)
                if best_name and best_name[3] is not None else "",
            })

    rows.sort(key=lambda r: (-(r["km_to_pin"] == "" and 0 or 1),
                             r["km_to_pin"] if r["km_to_pin"] != "" else 999))
    with REPORT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    likely = [r for r in rows
              if r["km_to_pin"] != "" and r["km_to_pin"] <= NEAR_KM
              and r["name_similarity"] >= NAME_FLOOR]
    near_only = [r for r in rows
                 if r["km_to_pin"] != "" and r["km_to_pin"] <= NEAR_KM
                 and r not in likely]
    print(f"\n{len(rows)} source locations carry bids but reach no pin")
    print(f"  {len(likely)} sit near an unmatched pin AND read like it "
          f"- probable missed matches")
    print(f"  {len(near_only)} sit near an unmatched pin but read differently "
          f"- check by hand")
    print(f"\nwrote {REPORT}")
    for row in likely:
        print(f"  {row['km_to_pin']:6.2f} km  {row['source_location'][:30]:32s}"
              f" -> {row['nearest_free_pin'][:38]:40s} [{row['source']}]")


if __name__ == "__main__":
    main()
