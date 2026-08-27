#!/usr/bin/env python3
"""Link map pins to the scraped source locations. Run this by hand, not on a schedule.

The map's pins are hand-curated and have no stable ID, so this script proposes a
pinId -> (source, source_location_id) mapping and writes it to
``scrapers/config/location_map.json``. **Review that file before committing it** -
after it is committed the scheduled build is deterministic and never guesses.

Matching strategy, in order of trust:

1. Each pin carries a ``company`` (and usually a ``url`` to its bid page). Either
   one identifies which co-op owns the pin, which narrows the candidates to a
   single source before any fuzzy matching happens. That constraint is what
   keeps the rest reliable - we never compare a pin against another co-op's
   locations.
2. Within that source, prefer geography: AgriCharts publishes latitude and
   longitude per location, so a pin within ~8 km of exactly one candidate is an
   unambiguous match.
3. Fall back to name similarity for sources with no coordinates (Heartland).

Usage:
  python scrapers/build_bids.py --raw          # produce scrapers/.build/raw.json first
  python scrapers/match_locations.py
  python scrapers/match_locations.py --report  # show what matched and what did not
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import sys
from urllib.parse import urlparse

REPO = pathlib.Path(__file__).resolve().parent.parent
RAW = REPO / "scrapers" / ".build" / "raw.json"
OUTPUT = REPO / "scrapers" / "config" / "location_map.json"

# Candidate files holding the canonical `gtLocations` array, best first.
HTML_CANDIDATES = [
    REPO / "grain-trucking-map.html",
    REPO / "archive" / "2026-08-13-1237.html",
]

# Which source owns a pin, keyed by the pin's `company` field. This is the
# primary resolver: it is curated by hand in the map, so it stays correct even
# when a co-op changes its bid-page URL.
COMPANY_TO_SOURCE = {
    "Heartland Coop": "heartland",
    "CVA": "agricharts:cvacoop",
    "Gold-Eagle Coop": "agricharts:goldeagle",
    "Pro Coop": "agricharts:procooperative",
    "North Iowa Coop": "agricharts:nicoop",
    "Mid-Iowa Milling": "agricharts:midiowa",
    "New Coop": "newcoop",
    "Key Cooperative": "agricharts:keycoop",
    "CGB": "agricharts:cgb",
    "Innovative Ag Services": "agricharts:innovativeag",
    "Pine Lake Corn Processors": "agricharts:innovativeag",
    "CFE": "agricharts:coopfe",
    "Stateline Cooperative": "agricharts:statelinecoop",
    "Tama-Benton Cooperative": "agricharts:tamabentoncoop",
    "Two Rivers Cooperative": "agricharts:tworivers",
    "SilverEdge Cooperative": "agricharts:silveredgecoop",
    "JBS": "agricharts:jbslivepork",
}

# Fallback resolver, keyed by the host of the pin's existing bid URL, for pins
# whose company name isn't in the table above.
HOST_TO_SOURCE = {
    "myaccount.heartlandcoop.com": "heartland",
    "www.cvacoop.com": "agricharts:cvacoop",
    "cvacoop.com": "agricharts:cvacoop",
    "goldeaglecoop.com": "agricharts:goldeagle",
    "www.goldeaglecoop.com": "agricharts:goldeagle",
    "procooperative.com": "agricharts:procooperative",
    "www.procooperative.com": "agricharts:procooperative",
    "www.midiowacoop.com": "agricharts:midiowa",
    "midiowacoop.com": "agricharts:midiowa",
    "nicoop.com": "agricharts:nicoop",
    "www.nicoop.com": "agricharts:nicoop",
}

MAX_MATCH_KM = 8.0

# Words that carry no identifying signal when comparing facility names.
STOPWORDS = {
    "coop", "co", "op", "cooperative", "inc", "llc", "the", "grain", "elevator",
    "ethanol", "plant", "feed", "mill", "terminal", "energy", "ag", "farms",
    "farm", "company", "of", "and", "ia", "iowa", "llp", "lp",
}

# `company` is optional so this still parses the older archive files, which
# predate that field.
_LOCATION_RE = re.compile(
    r"\{\s*name:\s*'((?:[^'\\]|\\.)*)'\s*,\s*type:\s*'([^']*)'\s*,"
    r"(?:\s*company:\s*'((?:[^'\\]|\\.)*)'\s*,)?"
    r"\s*lat:\s*(-?[\d.]+)\s*,\s*lng:\s*(-?[\d.]+)\s*(?:,\s*url:\s*'([^']*)')?"
)


def find_html() -> pathlib.Path:
    for path in HTML_CANDIDATES:
        if path.exists() and _LOCATION_RE.search(path.read_text(encoding="utf-8", errors="replace")):
            return path
    raise SystemExit(
        "no file with a gtLocations array found. Export the live embed from "
        "Squarespace to grain-trucking-map.html first."
    )


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "pin"


def load_pins(path: pathlib.Path) -> list[dict]:
    """Parse gtLocations, assigning each pin a stable slug id."""
    text = path.read_text(encoding="utf-8", errors="replace")
    pins: list[dict] = []
    seen: dict[str, int] = {}

    for match in _LOCATION_RE.finditer(text):
        name = match.group(1).replace("\\'", "'").strip()
        base = slugify(name)
        seen[base] = seen.get(base, 0) + 1
        pin_id = base if seen[base] == 1 else base + "-" + str(seen[base])
        company = (match.group(3) or "").replace("\\'", "'").strip()
        url = match.group(6) or ""
        try:
            host = urlparse(url).netloc.lower()
        except ValueError:
            host = ""
        pins.append({
            "id": pin_id,
            "name": name,
            "type": match.group(2),
            "company": company,
            "lat": float(match.group(4)),
            "lng": float(match.group(5)),
            "url": url,
            "host": host,
        })
    return pins


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    meaningful = {w for w in words if w not in STOPWORDS and len(w) > 1}
    return meaningful or set(words)


def name_score(a: str, b: str) -> float:
    """Containment-biased token overlap, 0..1.

    Pin names are verbose ("gold eagle coop hutchins") while source names are
    terse ("Hutchins"), so plain Jaccard under-scores real matches; containment
    of the smaller token set is the signal that matters.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    overlap = len(ta & tb)
    if not overlap:
        return 0.0
    return overlap / min(len(ta), len(tb))


def match_pin(pin: dict, candidates: list[dict]) -> tuple[dict | None, float, str]:
    """Return (best_candidate, confidence, method)."""
    if not candidates:
        return None, 0.0, "no-candidates"

    # 1. Geography, where the source publishes it.
    geo = [c for c in candidates if c.get("latitude") and c.get("longitude")]
    if geo:
        scored = sorted(
            ((haversine_km(pin["lat"], pin["lng"], c["latitude"], c["longitude"]), c)
             for c in geo),
            key=lambda t: t[0],
        )
        best_km, best = scored[0]
        if best_km <= MAX_MATCH_KM:
            runner_up_km = scored[1][0] if len(scored) > 1 else float("inf")
            # Confidence falls off with distance, and drops further when a
            # second candidate sits at a comparable distance.
            confidence = max(0.0, 1.0 - best_km / MAX_MATCH_KM)
            if runner_up_km < best_km * 2:
                confidence *= 0.6
            # A strong name agreement rescues an otherwise marginal distance.
            confidence = max(confidence, name_score(pin["name"], best["name"]))
            return best, round(min(confidence, 1.0), 3), "geo(%.1fkm)" % best_km

    # 2. Name similarity.
    scored_names = sorted(
        ((name_score(pin["name"], c["name"]), c) for c in candidates),
        key=lambda t: t[0],
        reverse=True,
    )
    best_score, best = scored_names[0]
    if best_score >= 0.5:
        runner_up = scored_names[1][0] if len(scored_names) > 1 else 0.0
        if runner_up >= best_score:
            # Ambiguous: two sources share the name. Flag for manual review.
            return best, round(best_score * 0.5, 3), "name-ambiguous"
        return best, round(best_score, 3), "name"

    return None, 0.0, "no-match"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="print the full match report")
    parser.add_argument("--min-confidence", type=float, default=0.35,
                        help="below this a match is recorded as needing review")
    args = parser.parse_args()

    if not RAW.exists():
        raise SystemExit("missing " + str(RAW) + " - run: python scrapers/build_bids.py --raw")

    raw = json.loads(RAW.read_text(encoding="utf-8"))["sources"]
    html_path = find_html()
    pins = load_pins(html_path)
    print("pins: " + str(len(pins)) + " (from " + html_path.name + ")")
    print("sources: " + ", ".join(str(k) + "=" + str(len(v)) for k, v in raw.items()))

    mapping: dict[str, dict] = {}
    review: list[dict] = []
    unmatched: list[dict] = []
    no_source: list[dict] = []

    for pin in pins:
        source_id = COMPANY_TO_SOURCE.get(pin["company"]) or HOST_TO_SOURCE.get(pin["host"])
        if source_id is None or source_id not in raw:
            if pin["type"] != "farm":
                no_source.append(pin)
            continue

        best, confidence, method = match_pin(pin, raw[source_id])
        if best is None:
            unmatched.append({**pin, "source": source_id, "method": method})
            continue

        entry = {
            "name": pin["name"],
            "source": source_id,
            "source_location_id": best["source_location_id"],
            "source_name": best["name"],
            "confidence": confidence,
            "method": method,
        }
        if confidence < args.min_confidence:
            # Deliberately NOT added to `pins`. Showing a farmer the wrong
            # elevator's bid is worse than showing none, so a weak match is
            # parked for a human to confirm and promote by hand.
            review.append({**entry, "pin_id": pin["id"]})
        else:
            mapping[pin["id"]] = entry

    payload = {
        "_comment": (
            "Generated by match_locations.py - REVIEW BEFORE COMMITTING. "
            "Edit source_location_id by hand where the match is wrong, then set "
            "confidence to 1.0 to record that a human verified it."
        ),
        "generated_from": html_path.name,
        "pins": dict(sorted(mapping.items())),
        "needs_review": sorted(review, key=lambda e: e["confidence"]),
        "unmatched": unmatched,
        "no_source_configured": [
            {"id": p["id"], "name": p["name"], "company": p["company"], "host": p["host"]}
            for p in no_source
        ],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\nmatched:        " + str(len(mapping)) + " pins")
    print("held back:      " + str(len(review)) + " (confidence < " + str(args.min_confidence) + ", not published)")
    print("unmatched:      " + str(len(unmatched)) + " (source known, no location found)")
    print("no source yet:  " + str(len(no_source)) + " (host not covered by an adapter)")
    print("wrote " + str(OUTPUT.relative_to(REPO)))

    if args.report:
        by_method: dict[str, int] = {}
        for entry in mapping.values():
            key = entry["method"].split("(")[0]
            by_method[key] = by_method.get(key, 0) + 1
        print("\nmatch methods: " + str(by_method))

        if review:
            print("\n--- low confidence, check these ---")
            for entry in review[:40]:
                print("  %.2f  %-40s -> %-28s [%s]" % (
                    entry["confidence"], entry["name"][:40],
                    entry["source_name"][:28], entry["method"]))
        if unmatched:
            print("\n--- unmatched ---")
            for pin in unmatched[:40]:
                print("  %-40s  source=%s" % (pin["name"][:40], pin["source"]))
        if no_source:
            hosts: dict[str, int] = {}
            for pin in no_source:
                hosts[pin["host"]] = hosts.get(pin["host"], 0) + 1
            print("\n--- pins whose host has no adapter yet ---")
            for host, count in sorted(hosts.items(), key=lambda t: -t[1]):
                print("  %-38s %d pins" % (host or "(no url)", count))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
