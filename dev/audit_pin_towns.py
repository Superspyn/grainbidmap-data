"""Check every pin sits near the town its own name claims.

    python dev/audit_pin_towns.py

The coordinate audit against the co-ops' feeds can only reach the 206 pins
whose source publishes a coordinate. This covers all 896, using the only
other thing every pin carries: a name, which nearly always ends in a town.

Against the US Census place gazetteer, a pin named "Key Coop Zearing Grain
Site IA" should be near Zearing, Iowa. If it is forty miles away, something
is wrong - either the pin or the name. It will not catch a pin that is a
mile off, which is what the feed comparison is for; it catches the gross
errors, which are the ones that make a haul estimate nonsense.

Two things it deliberately does not do. It does not move anything - a pin
gets moved with aerial imagery in front of you, because that is what
corrected the Five Star pins after both the state permit records and the
co-ops' own data had been wrong. And it does not treat a long distance as
proof: a facility genuinely sited out of town, or named after the co-op
rather than the town, will show up here and be perfectly correct.
"""
from __future__ import annotations

import csv
import math
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scrapers"))
import match_locations  # noqa: E402

PLACES = ROOT / "dev" / "us-places.csv"
REPORT = ROOT / "dev" / "pin-town-audit.csv"

# A facility is often a few miles out of the town it is named for - on the
# railway, or at the edge of the section. Past this it is worth a look.
LOOK_KM = 12.0

# Past this, a same-named town is a coincidence rather than evidence. There
# is an Ulmer in South Carolina and a Heartland in Texas; neither says
# anything about a pin in Iowa, and reporting 1,577 km as an error buries
# the real ones. Pins with no town of their name inside this radius are
# counted as unchecked, which is honest - not as passing.
PLAUSIBLE_KM = 250.0

STATES = {"IA", "MO", "IL", "MN", "NE", "SD", "WI", "KS", "OK", "AR", "MS",
          "LA", "TN", "KY", "IN", "OH", "MI", "ND", "TX", "AL", "CO"}

# Words that are never a town: company names, facility types, qualifiers.
NOISE = {
    "coop", "co", "op", "cooperative", "coops", "inc", "llc", "ltd", "company",
    "grain", "grains", "elevator", "elevators", "feed", "mill", "mills",
    "ethanol", "energy", "terminal", "terminals", "plant", "processing",
    "farmers", "farmer", "farm", "farms", "ag", "agri", "agriculture",
    "north", "south", "east", "west", "northeast", "northwest", "southeast",
    "southwest", "annex", "site", "hub", "river", "valley", "county",
    "the", "and", "of", "at", "new", "old", "st", "mt", "port", "landing",
    "approx", "bean", "soybean", "corn", "shuttle", "rail", "dry", "wet",
    "station", "depot", "yard", "center", "centre", "central", "main",
}


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def load_places() -> dict:
    """Town name -> list of (state, lat, lon), lowercased."""
    places: dict[str, list] = {}
    with PLACES.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = row["name"].lower()
            # The gazetteer carries "Zearing city"; the bare name is what a
            # pin will be named after.
            key = re.sub(r"\s+(city|town|village|borough|cdp|township)$", "", key)
            places.setdefault(key, []).append(
                (row["state"], float(row["lat"]), float(row["lon"])))
    return places


def candidate_towns(name: str, company: str = "") -> tuple[list[str], str | None]:
    """Plausible town names inside a pin name, longest phrase first.

    Tried longest-first so "Prairie City" is preferred over "Prairie", and
    so a two-word town is not missed because its first word is also a town
    somewhere else.

    The company's own words are dropped first. Without that, "Heartland Coop
    Voorhies" matches Heartland, Texas - the company name beat the town it
    was actually named after, and eight pins came back over 500 km "wrong"
    for no reason but their owner's name.
    """
    company_words = {w.lower() for w in re.split(r"[^A-Za-z]+", company) if w}
    words = [w for w in re.split(r"[^A-Za-z]+", name) if w]
    trailing_state = words[-1].upper() if words and words[-1].upper() in STATES else None
    if trailing_state:
        words = words[:-1]
    skip = NOISE | company_words
    out = []
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            chunk = words[i:i + size]
            if all(w.lower() in skip for w in chunk):
                continue
            out.append(" ".join(chunk).lower())
    return out, trailing_state


def main() -> None:
    if not PLACES.exists():
        sys.exit(f"No {PLACES}. It is the US Census place gazetteer, reduced to\n"
                 "  state,name,lat,lon - see the commit that added this file.")
    places = load_places()
    pins = match_locations.load_pins(match_locations.find_html())
    print(f"{len(pins)} pins, {len(places)} distinct place names")

    rows, unchecked = [], []
    for pin in pins:
        names, state = candidate_towns(pin["name"], pin.get("company", ""))
        best = None
        for phrase in names:
            for st, lat, lon in places.get(phrase, []):
                if state and st != state:
                    continue
                d = km(pin["lat"], pin["lng"], lat, lon)
                if d > PLAUSIBLE_KM:
                    continue   # a coincidence of names, not this pin's town
                if best is None or d < best[0]:
                    best = (d, phrase, st)
            if best and best[0] <= LOOK_KM:
                break          # a good match on a longer phrase settles it
        if best is None:
            unchecked.append(pin["name"])
            continue
        rows.append({"pin_id": pin["id"], "pin_name": pin["name"],
                     "town": best[1], "state": best[2],
                     "km": round(best[0], 2),
                     "lat": pin["lat"], "lng": pin["lng"]})

    rows.sort(key=lambda r: -r["km"])
    with REPORT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    far = [r for r in rows if r["km"] >= LOOK_KM]
    print(f"  {len(rows)} pins sit near a town matching their own name")
    print(f"  {len(unchecked)} could not be checked - no town of that name "
          f"within {PLAUSIBLE_KM:.0f} km")
    print(f"  {len(far)} sit more than {LOOK_KM} km from that town")
    if rows:
        print(f"  median distance to the named town: "
              f"{sorted(r['km'] for r in rows)[len(rows) // 2]:.2f} km")
    print(f"\nwrote {REPORT}")
    for row in far[:30]:
        print(f"  {row['km']:8.1f} km  {row['pin_name'][:44]:46s} "
              f"named for {row['town']}, {row['state']}")


if __name__ == "__main__":
    main()
