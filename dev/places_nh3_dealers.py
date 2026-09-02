"""Pin each dealer on the actual business, and pick up its website, via the
Google Places API (New).

The state license list gives an address and nothing else, and for 78 locations
not even that - just a PO box. Free geocoders can only interpolate along a
street centreline, and OpenStreetMap has almost no coverage of rural Iowa
co-op sites. Places knows these businesses by name, so one searchText call per
row returns the facility's own coordinates AND its website.

KEY: this needs a *server-side* key. The key in nh3-map.html is HTTP-referrer
restricted (right for a key that ships inside a public page) and Google
refuses it here. Make a second key in the same project, restrict it to the
Places API - and by IP if you like - then put it in grain-map/.places-key,
which is gitignored. Never paste that key into the HTML.

Cost: one searchText call per row with a field mask covering location and
website. Check current Places pricing and your free monthly allowance before
running the full set; --limit N does a small trial first.

Usage:
    python dev/places_nh3_dealers.py --limit 10     # trial run
    python dev/places_nh3_dealers.py --approx-only  # just the town-centre pins
    python dev/places_nh3_dealers.py                # everything

Updates dev/nh3-dealers-geocoded.csv in place; results are cached in
dev/.places-cache.json so a re-run costs nothing for rows already looked up.
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent

# Credentials live outside the repo. This repo is public, and a secret only
# stays out of it while .gitignore is remembered - moving it out of the tree
# entirely means a stray "git add -A" cannot reach it at all.
SECRETS_DIR = Path.home() / ".grain-map-secrets"
SRC_PATH = HERE / "nh3-dealers-filtered.csv"
CSV_PATH = HERE / "nh3-dealers-geocoded.csv"
CACHE = HERE / ".places-cache.json"
# Legacy Places, because that's what's enabled on this project - "Places API",
# not "Places API (New)". Two calls per dealer: Find Place resolves the name to
# a place_id + coordinates, then Details fetches the website. Flip USE_NEW to
# True if Places API (New) is ever enabled; the new one does both in one call.
USE_NEW = False
ENDPOINT_NEW = "https://places.googleapis.com/v1/places:searchText"
FIELDS_NEW = "places.displayName,places.formattedAddress,places.location,places.websiteUri,places.businessStatus"
ENDPOINT_FIND = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
ENDPOINT_DETAILS = "https://maps.googleapis.com/maps/api/place/details/json"

# A Places hit further than this from the address we already have is a
# different business with a similar name, not our dealer.
MAX_DRIFT_MI = 15.0


def load_key() -> str:
    """Find the Places key, preferring locations outside the repo.

    This is a server-side key with no referrer restriction, so anyone holding
    it can bill Google against this account. It used to live at the repo root
    where a stray ``git add -A`` would publish it - this repo is public - so
    the home-directory path is now the preferred home and the old location is
    only a fallback, kept so an existing checkout does not break.
    """
    key = os.environ.get("GOOGLE_PLACES_KEY")
    if key:
        return key.strip()
    for f in (SECRETS_DIR / "places-key", HERE.parent / ".places-key"):
        if f.exists():
            return f.read_text(encoding="utf-8").strip()
    sys.exit(
        "No Places key found.\n"
        "  Put a server-side key (no HTTP-referrer restriction) in "
        f"{SECRETS_DIR / 'places-key'}\n"
        "  or set GOOGLE_PLACES_KEY in the environment."
    )


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 3958.8
    dlat, dlng = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    s = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(s), math.sqrt(1 - s))


def clean_name(facility: str) -> str:
    """Trim the state list's site-code noise that Places has never heard of."""
    s = re.sub(r"#\s*\d+", "", facility)
    s = re.sub(r"\s+-\s+.*$", "", s)      # 'GOLD-EAGLE COOP - HOLMES 2150 ...'
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -,")


def _get_json(url: str, params: dict) -> dict | None:
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(full, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503):
                time.sleep(2 * (attempt + 1))
                continue
            print(f"  HTTP {e.code}: {e.read().decode()[:200]}")
            return None
        except Exception:
            time.sleep(2)
    return None


def search_legacy(key: str, text: str, near: tuple[float, float] | None) -> dict | None:
    """Find Place, then Details for the website.

    Returns the same shape the new API gives us, so the caller doesn't care
    which one answered.
    """
    found = _get_json(ENDPOINT_FIND, {
        "input": text,
        "inputtype": "textquery",
        "fields": "name,formatted_address,geometry,place_id,business_status",
        # circle:radius@lat,lng - a bias, so a good match just outside still returns
        "key": key,
    })
    if found is None:
        return None
    status = found.get("status")
    if status == "ZERO_RESULTS":
        return {}
    if status != "OK":
        msg = found.get("error_message", "")
        if status in ("REQUEST_DENIED", "OVER_QUERY_LIMIT"):
            sys.exit(f"Places refused the request ({status}): {msg}")
        return {}
    cand = (found.get("candidates") or [{}])[0]
    if not cand:
        return {}
    loc = (cand.get("geometry") or {}).get("location") or {}
    out = {
        "displayName": {"text": cand.get("name", "")},
        "formattedAddress": cand.get("formatted_address", ""),
        "location": {"latitude": loc.get("lat"), "longitude": loc.get("lng")},
        "businessStatus": cand.get("business_status", ""),
    }
    pid = cand.get("place_id")
    if pid:
        det = _get_json(ENDPOINT_DETAILS, {"place_id": pid, "fields": "website", "key": key})
        if det and det.get("status") == "OK":
            site = (det.get("result") or {}).get("website")
            if site:
                out["websiteUri"] = site
    return out


def search(key: str, text: str, near: tuple[float, float] | None = None) -> dict | None:
    if not USE_NEW:
        return search_legacy(key, text, near)
    body = json.dumps({
        "textQuery": text,
        "maxResultCount": 1,
        # Bias, not restrict: a nearby match wins but a legitimate hit just
        # outside the circle still comes back and gets distance-checked below.
        "locationBias": {"circle": {
            "center": {"latitude": near[0], "longitude": near[1]},
            "radius": 30000.0,
        }},
    }).encode()
    req = urllib.request.Request(ENDPOINT_NEW, data=body, headers={
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": FIELDS_NEW,
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            places = data.get("places") or []
            return places[0] if places else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in (429, 500, 503):
                time.sleep(2 * (attempt + 1))
                continue
            print(f"  HTTP {e.code}: {detail}")
            if e.code == 403:
                sys.exit("Key rejected - see the KEY note at the top of this file.")
            return None
        except Exception:
            time.sleep(2)
    return None


def queries_for(r: dict) -> list[str]:
    """Query candidates, most specific first.

    Name-plus-town alone makes Places hand back the chain's most prominent
    branch, which for "Nutrien Ag Solutions, Brayton" is a Nutrien 60 miles
    away. Leading with the street address pins the actual site; the bare
    address last still places the pin even when Places doesn't know the
    business by name.
    """
    name = clean_name(r["facility"])
    street = (r.get("geocode_street") or "").strip()
    city, zipcode = r["city"], (r.get("zip") or "").split("-")[0]
    out = []
    if any(ch.isdigit() for ch in street):
        out.append(f"{name}, {street}, {city}, IA {zipcode}")
    out.append(f"{name}, {city}, IA {zipcode}")
    if any(ch.isdigit() for ch in street):
        out.append(f"{street}, {city}, IA {zipcode}")
    return out


# Places answers a name query with the most plausible thing in that town, and
# in a town of 200 that can be the wrong business entirely: "New Century FS,
# Boxholm" came back as The Dog House. So the returned name has to look like
# the licensee we asked for - unless it's one of these, where the licensee and
# the sign on the building genuinely differ.
ALIASES = [
    ({"nutrien"}, {"crop", "production", "services", "cps"}),
    ({"new", "coop"}, {"maxyield"}),
    ({"new", "cooperative"}, {"maxyield"}),
    ({"premier"}, {"farmers", "union"}),
    ({"agstate"}, {"first", "cooperative", "association"}),
    ({"agstate"}, {"farmers", "cooperative"}),
    ({"growmark"}, {"fs"}),
]
STOPWORDS = {"inc", "llc", "llp", "co", "company", "corp", "the", "of", "and",
             "ia", "iowa", "cooperative", "coop", "co-op", "ag", "agri"}
ADDRESS_LIKE = re.compile(r"^\d|^[A-Za-z]+ \d+ ?&|^(Iowa|US|Hwy|Highway|County|Co) ", re.I)


def tokens(s: str) -> set[str]:
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", s.lower()).split() if t and t not in STOPWORDS}


def name_ok(facility: str, matched: str) -> bool:
    """True when the business Places returned is plausibly the licensee.

    A bare street address counts: that's the address-only fallback query
    landing on the right spot, which is fine for a pin even though it isn't a
    business record.
    """
    if not matched:
        return False
    if ADDRESS_LIKE.match(matched.strip()):
        return True
    a, b = tokens(facility), tokens(matched)
    if not a or not b:
        return False
    if len(a & b) / min(len(a), len(b)) >= 0.34:
        return True
    for left, right in ALIASES:
        if (left & a and right & b) or (left & b and right & a):
            return True
    return False


CITY_STATE_ZIP = re.compile(r"^([A-Z]{2})\s+(\d{5})(?:-\d{4})?$")


def norm_city(c: str) -> str:
    """Squash the spellings the two sources disagree about.

    The state list writes LUVERNE and IDAGROVE; Google writes Lu Verne and Ida
    Grove. Mount/Mt and Saint/St swap freely in both directions.
    """
    c = c.lower().replace(".", " ")
    c = re.sub(r"mt", "mount", c)
    c = re.sub(r"st", "saint", c)
    return re.sub(r"[^a-z]", "", c)


def split_address(formatted: str) -> tuple[str, str]:
    """Pull (city, zip) out of '545 225th St, Britt, IA 50423, USA'.

    Substring matching on the whole address is not good enough: 'Rake' is
    inside 'Drake St, Des Moines', 'Elk' is inside 'Elkader', and a rural
    house number like 50245 looks exactly like a ZIP. The city has to be
    compared as its own field.
    """
    parts = [p.strip() for p in formatted.split(",") if p.strip()]
    for i, part in enumerate(parts):
        m = CITY_STATE_ZIP.match(part)
        if m and i >= 1:
            return parts[i - 1], m.group(2)
    # no 'IA 50423' component - fall back to the second-to-last field
    if len(parts) >= 2:
        return parts[-2], ""
    return "", ""


def city_matches(formatted: str, city: str, zipcode: str) -> bool:
    got_city, got_zip = split_address(formatted)
    if not got_city:
        return False
    if norm_city(got_city) == norm_city(city):
        return True
    # a neighbouring-town postal address on the same ZIP is still the right place
    want_zip = (zipcode or "").split("-")[0]
    return bool(want_zip) and got_zip == want_zip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only process N rows (trial run)")
    ap.add_argument("--force", action="store_true", help="re-look-up rows an earlier run already located")
    args = ap.parse_args()

    key = load_key()
    # Read the FILTERED list, not the geocoded one: Places locates every row
    # by name, so it doesn't need the free geocoders to have gone first.
    rows = list(csv.DictReader(SRC_PATH.open(encoding="utf-8")))
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}

    # Carry forward anything an earlier run already pinned down, so a re-run
    # only spends calls on the rows that are still unlocated.
    prior: dict[tuple, dict] = {}
    if CSV_PATH.exists() and not args.force:
        for old in csv.DictReader(CSV_PATH.open(encoding="utf-8")):
            if old.get("lat"):
                prior[(old["facility"], old["street"], old["city"])] = old

    todo = rows[:args.limit] if args.limit else rows
    print(f"{len(todo)} rows; {len(prior)} already located, {len(cache)} cached queries", flush=True)

    located = sited = missed = wrongtown = 0
    for n, r in enumerate(todo, 1):
        r.setdefault("website", "")
        r["approx"] = "1"
        r["lat"] = r["lng"] = ""

        was = prior.get((r["facility"], r["street"], r["city"]))
        if was:
            for f in ("lat", "lng", "approx", "located_by", "website", "matched_name", "status"):
                if was.get(f):
                    r[f] = was[f]
            located += 1
            if was.get("website"):
                sited += 1
            continue

        accepted = False
        for query in queries_for(r):
            if query in cache:
                hit = cache[query]
            else:
                hit = search(key, query, None)
                if hit is None:
                    break
                cache[query] = hit
            if not hit:
                continue
            if not city_matches(hit.get("formattedAddress", ""), r["city"], r["zip"]):
                continue
            matched_name = (hit.get("displayName") or {}).get("text", "")
            if not name_ok(r["facility"], matched_name):
                continue
            loc = hit.get("location") or {}
            if loc.get("latitude") is None:
                continue
            r["lat"] = f'{loc["latitude"]:.6f}'
            r["lng"] = f'{loc["longitude"]:.6f}'
            r["approx"] = "0"
            r["located_by"] = "places"
            r["matched_name"] = matched_name
            if hit.get("websiteUri"):
                r["website"] = hit["websiteUri"]
                sited += 1
            if hit.get("businessStatus", "").startswith("CLOSED"):
                r["status"] = hit["businessStatus"]
            located += 1
            accepted = True
            break

        if not accepted:
            wrongtown += 1

        if n % 50 == 0:
            CACHE.write_text(json.dumps(cache), encoding="utf-8")
            print(f"  ...{n}/{len(todo)}  located {located}, websites {sited}, "
                  f"unresolved {wrongtown + missed}", flush=True)

    CACHE.write_text(json.dumps(cache), encoding="utf-8")

    fields = list(rows[0].keys())
    for extra in ("approx", "lat", "lng", "located_by", "website", "matched_name", "status"):
        if extra not in fields:
            fields.append(extra)
    # Write via a temp file and replace, so a crash or a kill mid-write can
    # never leave a half-written CSV behind (it did once).
    tmp = CSV_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(CSV_PATH)

    print("")
    print(f"located {located} of {len(todo)} on the actual business; {sited} websites from Places")
    print(f"no Places match: {missed} | matched a different town, rejected: {wrongtown}")
    print(f"rows left without coordinates: {sum(1 for r in rows if not r.get('lat'))}")


if __name__ == "__main__":
    main()
