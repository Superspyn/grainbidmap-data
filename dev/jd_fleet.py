"""Pull fields and truck positions from Operations Center to a private file.

Written for the farm PC to run unattended: it refreshes the access token
itself using the stored refresh token, so no browser is involved after the
first sign-in.

    python dev/jd_fleet.py

Output goes OUTSIDE the repo, to

    %USERPROFILE%\\.grain-map-secrets\\fleet.json

deliberately. This repo is public and served by GitHub Pages, and live truck
positions say when the yard is empty and where the equipment sits overnight.
Nothing here writes into the repo; publishing is a separate decision.

Deere has no CORS headers - a browser preflight from the site's own origin
comes back 401 with no Access-Control-Allow-Origin - so the map can never call
Deere directly. Something we run has to fetch it, which is this.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SECRETS = pathlib.Path.home() / ".grain-map-secrets"
CONFIG = SECRETS / "johndeere.json"
TOKEN_CACHE = SECRETS / "johndeere-token.json"
OUTPUT = SECRETS / "fleet.json"

TOKEN_URL = "https://signin.johndeere.com/oauth2/aus78tnlaysMraFhC1t7/v1/token"
API = "https://api.deere.com"
ACCEPT = "application/vnd.deere.axiom.v3+json"

# Equipment records whose type says they are road vehicles rather than field
# machines. Deere labels the Kenworths and Macks "Truck".
TRUCK_TYPES = {"truck", "trailer", "pickup", "semi"}


def _read(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def refresh_token() -> str:
    """Swap the stored refresh token for a fresh access token.

    Tries the client secret first and falls back to PKCE-style public client,
    since Deere accepts 'none' for token endpoint auth.
    """
    cfg = _read(CONFIG)
    tok = _read(TOKEN_CACHE)
    payload = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
               "scope": tok.get("scope", "")}

    attempts = []
    if cfg.get("client_secret"):
        attempts.append((dict(payload), (cfg["client_id"], cfg["client_secret"])))
    attempts.append(({**payload, "client_id": cfg["client_id"]}, None))

    last = None
    for body, auth in attempts:
        req = urllib.request.Request(
            TOKEN_URL, data=urllib.parse.urlencode(body).encode(), method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Accept", "application/json")
        if auth:
            raw = f"{auth[0]}:{auth[1]}".encode()
            req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode())
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                new = json.loads(r.read().decode())
            # Deere does not always return a new refresh token.
            new.setdefault("refresh_token", tok["refresh_token"])
            TOKEN_CACHE.write_text(json.dumps(new, indent=1), encoding="utf-8")
            try:
                os.chmod(TOKEN_CACHE, 0o600)
            except OSError:
                pass
            return new["access_token"]
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} {e.read().decode()[:150]}"
    sys.exit(f"could not refresh the Deere token: {last}\n"
             "  run dev/jd_explore.py to sign in again")


def api(token: str, url: str):
    """GET, returning (status, body). Never raises on an HTTP error."""
    if not url.startswith("http"):
        url = API + url
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", ACCEPT)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def api_all(token: str, url: str) -> list[dict]:
    """Every page of a list endpoint, not just the first.

    Deere returns ten items by default and puts the rest behind a nextPage
    link; itemLimit raises that but caps at 100. Reading only the first page
    made a 225-field organization look like a 10-field one, which is the kind
    of wrong that looks like real data.
    """
    joiner = "&" if "?" in url else "?"
    url = f"{url}{joiner}itemLimit=100"
    seen: list[dict] = []
    guard = 0
    while url and guard < 200:
        guard += 1
        status, body = api(token, url)
        if status != 200 or not isinstance(body, dict):
            break
        seen.extend(body.get("values", []))
        nxt = [l.get("uri") for l in body.get("links", [])
               if l.get("rel") == "nextPage"]
        url = nxt[0] if nxt else None
    return seen


def now_iso() -> str:
    return (_dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def connected_orgs(token: str) -> list[dict]:
    _s, body = api(token, "/platform/organizations")
    if not isinstance(body, dict):
        return []
    # An org still carrying a "connections" link has not granted access yet.
    return [o for o in body.get("values", [])
            if not any(l.get("rel") == "connections" for l in o.get("links", []))]


HECTARES_TO_ACRES = 2.4710538

# Deere's "simplified" boundary still runs to about 1,400 points per field -
# survey grade, and 224 fields of it came to 16.8 MB. This map needs a field
# outline only to recognise the shape; the centroid is what prices the haul.
MAX_RING_POINTS = 48


def _acres(measurement: dict | None) -> float | None:
    """Deere reports area as valueAsDouble, usually in hectares."""
    if not isinstance(measurement, dict):
        return None
    value = measurement.get("valueAsDouble")
    if value is None:
        value = measurement.get("value")
    if not isinstance(value, (int, float)):
        return None
    unit = str(measurement.get("unit") or "").lower()
    if unit in ("ha", "hectare", "hectares"):
        value *= HECTARES_TO_ACRES
    elif unit in ("m2", "sqm", "square metre", "square meter"):
        value *= 0.000247105
    return round(value, 1)


def _simplify(points: list[list[float]], keep: int = MAX_RING_POINTS) -> list[list[float]]:
    """Thin a ring to at most `keep` points, preserving its shape.

    Douglas-Peucker would be better but needs a tolerance tuned per field;
    even spacing along the ring is predictable, keeps the closing point, and
    is plenty for telling one field from another on a hauling map.
    """
    if len(points) <= keep:
        return points
    step = len(points) / float(keep - 1)
    thinned = [points[int(i * step)] for i in range(keep - 1)]
    thinned.append(points[-1])
    return thinned


def field_boundary(token: str, field: dict) -> dict:
    """Outline, centroid and acres for one field.

    Prefers the simplified boundary: the full one runs to thousands of points
    per field, which is detail a hauling map cannot show and would bloat the
    page it gets embedded in.
    """
    links = {l.get("rel"): l.get("uri") for l in field.get("links", [])}
    for rel in ("simplifiedBoundaries", "boundaries"):
        if rel not in links:
            continue
        status, body = api(token, links[rel])
        if status != 200 or not isinstance(body, dict):
            continue
        values = body.get("values") or [body]
        if not values:
            continue
        b = values[0]

        rings = []
        for poly in b.get("multipolygons") or []:
            for ring in poly.get("rings") or []:
                pts = [[round(p["lat"], 6), round(p["lon"], 6)]
                       for p in ring.get("points") or []
                       if p.get("lat") is not None and p.get("lon") is not None]
                if len(pts) >= 3:
                    rings.append(_simplify(pts))
        if not rings:
            continue

        centroid = b.get("centroid") or {}
        return {
            "rings": rings,
            "lat": centroid.get("lat"),
            "lon": centroid.get("lon"),
            "acres": _acres(b.get("workableArea")) or _acres(b.get("area")),
            "detail": rel,
        }
    return {"rings": [], "lat": None, "lon": None, "acres": None, "detail": None}


AEMP = "https://api.deere.com/fleet/{page}"
AEMP_NS = {"i": "http://standards.iso.org/iso/15143/-3"}

# Road vehicles, by OEM. Everything else in the feed is farm equipment.
SEMI_MAKES = {"MACK", "KENWORTH", "PETERBILT", "FREIGHTLINER",
              "INTERNATIONAL", "VOLVO", "WESTERN STAR"}
PICKUP_MAKES = {"CHEVROLET", "GMC", "FORD", "RAM", "DODGE", "TOYOTA", "NISSAN"}


def fleet_positions(token: str) -> list[dict]:
    """Every machine with a position, from the ISO 15143-3 (AEMP) feed.

    This is a self-contained fleet feed: one call returns each machine with
    its last known position, so it needs none of the /platform/machines routes
    - which is the point, since every one of those answers 403 on this account
    including the ones the console lists as approved.

    Note the position timestamp is an ATTRIBUTE on <Location>, not a child
    element; reading it as a child silently produced no ages at all.
    """
    out: list[dict] = []
    page = 1
    while page <= 50:
        req = urllib.request.Request(AEMP.format(page=page))
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/xml")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                xml = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if page == 1:
                print(f"  AEMP feed: HTTP {e.code}")
            break

        root = ET.fromstring(xml)
        found = root.findall(".//i:Equipment", AEMP_NS)
        if not found:
            break

        for eq in found:
            header = eq.find("i:EquipmentHeader", AEMP_NS)
            if header is None:
                continue
            make = (header.findtext("i:OEMName", "", AEMP_NS) or "").strip()
            kind = ("semi" if make.upper() in SEMI_MAKES else
                    "pickup" if make.upper() in PICKUP_MAKES else "equipment")
            loc = eq.find(".//i:Location", AEMP_NS)
            lat = lon = ts = None
            if loc is not None:
                lat = loc.findtext("i:Latitude", None, AEMP_NS)
                lon = loc.findtext("i:Longitude", None, AEMP_NS)
                ts = loc.get("datetime")
            out.append({
                "name": (header.findtext("i:EquipmentID", "", AEMP_NS) or "").strip(),
                "make": make,
                "model": (header.findtext("i:Model", "", AEMP_NS) or "").strip(),
                "vin": (header.findtext("i:SerialNumber", "", AEMP_NS) or "").strip(),
                "kind": kind,
                "lat": float(lat) if lat else None,
                "lon": float(lon) if lon else None,
                "at": ts,
            })

        nxt = [l for l in root.findall("i:Links", AEMP_NS)
               if (l.findtext("i:rel", "", AEMP_NS) or "").lower() == "next"]
        if not nxt:
            break
        page += 1
    return out


def main() -> None:
    token = refresh_token()
    orgs = connected_orgs(token)
    if not orgs:
        sys.exit("no connected organizations - grant access at "
                 "https://connections.deere.com/connections/"
                 f"{_read(CONFIG)['client_id']}/select-organizations")

    out = {"generated_at": now_iso(), "organizations": [], "fields": [], "trucks": []}
    refused = 0

    for org in orgs:
        oid = str(org["id"])
        out["organizations"].append({"id": oid, "name": org.get("name")})

        fields = api_all(token, f"/platform/organizations/{oid}/fields")
        print(f"  {org.get('name')}: {len(fields)} fields, reading boundaries...")
        for f in fields:
            if str(f.get("name") or "").strip() in ("", "---"):
                continue            # placeholder rows Deere carries
            entry = {"org": oid, "id": f.get("id"), "name": f.get("name")}
            entry.update(field_boundary(token, f))
            out["fields"].append(entry)

    # The AEMP feed is account-wide rather than per organization, so it is
    # read once rather than per org.
    for m in fleet_positions(token):
        if m["kind"] == "equipment":
            continue        # tractors, planters, sprayers - not road vehicles
        if m["lat"] is None or m["lon"] is None:
            refused += 1
            continue
        out["trucks"].append(m)

    OUTPUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    try:
        os.chmod(OUTPUT, 0o600)
    except OSError:
        pass

    semis = [t for t in out["trucks"] if t["kind"] == "semi"]
    pickups = [t for t in out["trucks"] if t["kind"] == "pickup"]
    outlined = sum(1 for f in out["fields"] if f.get("rings"))

    def freshest(vehicles):
        ages = []
        for v in vehicles:
            if not v.get("at"):
                continue
            try:
                t = _dt.datetime.fromisoformat(v["at"].replace("Z", "+00:00"))
            except ValueError:
                continue
            ages.append((_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() / 60)
        if not ages:
            return "no timestamps"
        ages.sort()
        return (f"freshest {ages[0]:.0f} min, "
                f"median {ages[len(ages) // 2] / 60:.1f} h")

    print(f"wrote {OUTPUT}")
    print(f"  organizations {len(out['organizations'])}")
    print(f"  fields        {len(out['fields'])}  ({outlined} with a boundary)")
    print(f"  semis         {len(semis):3d}  {freshest(semis)}")
    print(f"  pickups       {len(pickups):3d}  {freshest(pickups)}")
    if refused:
        print(f"  {refused} road vehicles had no position in the AEMP feed")


if __name__ == "__main__":
    main()
