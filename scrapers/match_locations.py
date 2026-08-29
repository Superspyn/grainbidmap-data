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
import difflib
import json
import math
import pathlib
import re
import sys
from urllib.parse import urlparse

REPO = pathlib.Path(__file__).resolve().parent.parent
RAW = REPO / "scrapers" / ".build" / "raw.json"
OUTPUT = REPO / "scrapers" / "config" / "location_map.json"
MANUAL = REPO / "scrapers" / "config" / "manual_matches.json"

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
    "Landus": "landus",
    "Nexus Cooperative": "nexus",
    "New Vision Co-op": "agricharts:newvision",
    # Name variants carried in by the Iowa DNR facility list, which spells
    # some co-ops differently from the map's own pins.
    "LANDUS COOPERATIVE": "landus",
    "Landus Cooperative": "landus",
    "Heartland CO-OP": "heartland",
    "New Cooperative  Inc.": "newcoop",
    "Innovative AG Services Co": "agricharts:innovativeag",
    "StateLine Cooperative": "agricharts:statelinecoop",
    "North Iowa Cooperative": "agricharts:nicoop",
    "Pro Cooperative": "agricharts:procooperative",
    "Meadowland Farmers Coop": "agricharts:meadowland",
    "Farmers Win Coop": "agricharts:farmerswin",
    "Hull Cooperative": "agricharts:hullcoop",
    "United Cooperatives (MO)": "agricharts:useunited",
    "Siouxland Energy Cooperative": "agricharts:siouxlandenergy",
    "Mid-Missouri Energy": "agricharts:midmissourienergy",
    "Farmers Elevator & Exchange": "agricharts:farmerselevator",
    "Ag Partners Cooperative": "agricharts:agpartners",
    "Butterfield & Associates Grain": "agricharts:butterfield",
    "Montrose Grain and Supply": "agricharts:montrose",
    "Craig Supply & Grain": "agricharts:craiggrain",
    "Farmers Elevator & Supply Company (Clinton)": "agricharts:farmerselevatorco",
    "Buchheit": "agricharts:buchheit",
    "Plymouth Energy": "agricharts:plymouthenergy",
    "Elite Octane": "cihedging:eliteoctane",
    "Little Sioux Corn Processors": "cihedging:littlesioux",
    # The DNR list spells this with "Inc"; the pin from the map does not.
    "Faas Feed & Grain Inc": "agricharts:faasfeed",
    "Hartog Elevator Inc": "agricharts:hartog",
    "Faas Feed & Grain": "agricharts:faasfeed",
    "CHS": "bushel:chs",
    "AGP": "bushel:agp",
    "Smithfield": "bushel:smithfield",
    "Golden Grain Energy": "cihedging:goldengrain",
    "POET": "gradable:poet",
    "Flint Hills Resources / POET": "gradable:poet",
    "ADM": "gradable:adm",
    "Cargill": "agricharts:cargillus",
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

# Deliberately below the default publish threshold so a tie is never published.
AMBIGUOUS_CONFIDENCE = 0.25

# How much clearer one tied candidate must be, character-wise, to win. Measured
# against the real ties in this data: "Creston 1" beats "Creston 2" by 0.074 and
# "EARLHAM" beats "EARLHAM FEED MILL" by 0.124, while ADM's "Quincy, IL
# (Elevator)" and "(Terminal)" are separated by exactly 0.000 and must stay
# unresolved.
TIEBREAK_MARGIN = 0.05

# Only break a tie when the token match was strong to begin with. A tie between
# weak candidates means the right answer probably is not in the list at all -
# Landus stops returning "Davis City" when their API errors on it, and the tied
# alternatives are all wrong. Breaking that tie promotes a wrong match.
TIEBREAK_MIN_TOKEN_SCORE = 0.75

# Two-letter state codes. These must not count as matching signal: without
# this, any two facilities in the same state share a token, which is enough to
# push an unrelated pair over the threshold (POET Glenville MN once matched
# Preston, MN on the strength of "MN" alone).
#
# Only the abbreviations, deliberately - full state names are excluded because
# several are also town names in this data set (Nevada, Iowa).
STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

# Words that carry no identifying signal when comparing facility names.
STOPWORDS = {
    "coop", "co", "op", "cooperative", "inc", "llc", "the", "grain", "elevator",
    "ethanol", "plant", "feed", "mill", "terminal", "energy", "ag", "farms",
    "farm", "company", "of", "and", "iowa", "llp", "lp",
} | STATE_CODES

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


def literal_ratio(a: str, b: str) -> float:
    """Character-level similarity of the two names, ignoring case and spacing.

    Used only to break a token-level tie: "New Coop Creston 1" and "Creston 1"
    share every meaningful token with "Creston 2", but the digit still tells
    them apart.
    """
    norm = lambda t: re.sub(r"[^a-z0-9]+", " ", str(t).lower()).strip()
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


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


# Words that appear in so many company names they identify nobody. A company
# left with none of its own words after this is skipped by the delivered-bid
# check rather than allowed to match on a generic word.
GENERIC_COMPANY_WORDS = {
    "ag", "agri", "coop", "cooperative", "corn", "company", "elevator",
    "energy", "ethanol", "farm", "farms", "feed", "grain", "grains", "inc",
    "llc", "mill", "milling", "processing", "products", "renewables",
    "service", "services", "supply",
    # Words that are place-names as often as company names. "New Coop" reduces
    # to "new", which would otherwise flag Cargill's own New Madrid.
    "new", "north", "south", "east", "west", "central", "united", "mid",
}


def names_another_company(pin_company: str, candidate_name: str,
                          companies: set[str]) -> str | None:
    """Return the other company a candidate is named for, if any.

    Co-ops quote *delivered* bids to other people's plants: Nexus publishes
    "Valero Charles City, IA", "AGP Manning, IA" and "Cargill Iowa Falls, IA"
    alongside its own elevators. Those are Nexus's bids for grain delivered
    there, and they belong to a different physical site - Nexus's own Charles
    City elevator is 6 km from Valero's plant, so pinning that bid on it would
    price a haul to the wrong place.

    Candidates only ever come from the pin's own co-op feed, so a candidate
    carrying a different company's name inside that feed is a delivered bid,
    not one of theirs. Two guards keep it from firing on ordinary town names:
    the candidate must *lead* with the company's name, the way every delivered
    bid here is written ("AGP Manning", "CARGILL - BLAIR", "Valero Charles
    City"), and generic words are stripped first.
    """
    ordered = re.sub(r"[^a-z0-9 ]+", " ", (candidate_name or "").lower()).split()
    words = set(ordered)
    first = ordered[0] if ordered else ""
    mine = set(re.sub(r"[^a-z0-9 ]+", " ", (pin_company or "").lower()).split())
    for company in companies:
        parts = [
            w for w in re.sub(r"[^a-z0-9 ]+", " ", company.lower()).split()
            if len(w) >= 3 and w not in GENERIC_COMPANY_WORDS
        ]
        # No distinctive word left means the name cannot identify anyone:
        # "Corn LP" reduces to "corn", which would flag every location called
        # "Clinton, IA (Corn Processing)".
        if not parts or set(parts) & mine:
            continue
        if first in parts and all(w in words for w in parts):
            return company
    return None


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
            # Token scores tie. Fall back to character-level similarity, which
            # sees the parts token filtering throws away (digits, suffixes).
            tied = [c for score, c in scored_names if score >= best_score]
            ranked = sorted(
                ((literal_ratio(pin["name"], c["name"]), c) for c in tied),
                key=lambda t: t[0],
                reverse=True,
            )
            if (
                best_score >= TIEBREAK_MIN_TOKEN_SCORE
                and len(ranked) > 1
                and ranked[0][0] - ranked[1][0] >= TIEBREAK_MARGIN
            ):
                return ranked[0][1], round(best_score, 3), "name-tiebreak"

            # Still indistinguishable - "Quincy, IL (Elevator)" against
            # "Quincy, IL (Terminal)". Park it rather than pick a coin flip;
            # scaling the score would let a perfect-but-tied name through.
            return best, AMBIGUOUS_CONFIDENCE, "name-ambiguous"
        return best, round(best_score, 3), "name"

    return None, 0.0, "no-match"


def load_manual() -> dict:
    """Hand-verified overrides, keyed by pin id. Comment keys are ignored."""
    if not MANUAL.exists():
        return {}
    data = json.loads(MANUAL.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


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
    delivered_skipped: list[tuple[str, str, str]] = []

    # Every company that owns a pin, used to spot a co-op's delivered bids to
    # someone else's plant.
    companies = {p["company"] for p in pins if p.get("company")}

    manual = load_manual()
    manual_used: set[str] = set()

    for pin in pins:
        # A hand-verified entry wins outright - it exists precisely because the
        # automatic matchers cannot see what the human could.
        override = manual.get(pin["id"])
        if override:
            src = override.get("source")
            loc_id = str(override.get("source_location_id"))
            known = {str(c["source_location_id"]) for c in raw.get(src, [])}
            if src not in raw:
                print("  manual_matches: pin %r names unknown source %r" % (pin["id"], src),
                      file=sys.stderr)
            elif loc_id not in known:
                print("  manual_matches: %r -> %s/%s is not a location that source "
                      "returned; ignoring" % (pin["id"], src, loc_id), file=sys.stderr)
            else:
                manual_used.add(pin["id"])
                mapping[pin["id"]] = {
                    "name": pin["name"],
                    "source": src,
                    "source_location_id": loc_id,
                    "source_name": override.get("source_name") or "",
                    "confidence": 1.0,
                    "method": "manual",
                }
                continue

        source_id = COMPANY_TO_SOURCE.get(pin["company"]) or HOST_TO_SOURCE.get(pin["host"])
        if source_id is None or source_id not in raw:
            if pin["type"] != "farm":
                no_source.append(pin)
            continue

        # Drop this co-op's delivered bids to other companies' plants before
        # scoring - they are real bids, but for a different physical site.
        candidates = []
        for candidate in raw[source_id]:
            other = names_another_company(pin["company"], candidate["name"], companies)
            if other is None:
                candidates.append(candidate)
            else:
                delivered_skipped.append((pin["id"], candidate["name"], other))

        best, confidence, method = match_pin(pin, candidates)
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

    # An entry that matched no pin is almost always a renamed pin: the id is a
    # slug of the pin's name, so renaming one orphans its override silently.
    orphans = sorted(set(manual) - manual_used)
    if orphans:
        print("\n  manual_matches: %d entr%s match no pin (renamed?): %s"
              % (len(orphans), "y" if len(orphans) == 1 else "ies", ", ".join(orphans)),
              file=sys.stderr)

    print("\nmatched:        " + str(len(mapping)) + " pins"
          + (" (" + str(len(manual_used)) + " manual)" if manual_used else ""))
    print("held back:      " + str(len(review)) + " (confidence < " + str(args.min_confidence) + ", not published)")
    print("unmatched:      " + str(len(unmatched)) + " (source known, no location found)")
    print("no source yet:  " + str(len(no_source)) + " (host not covered by an adapter)")
    if delivered_skipped:
        pairs = {(c, o) for _, c, o in delivered_skipped}
        print("delivered bids: " + str(len(pairs))
              + " source locations ignored as another company's plant")
        for cand, other in sorted(pairs)[:8]:
            print("                " + cand + "  (" + other + ")")
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
