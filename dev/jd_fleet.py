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
import math
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import jd_idle  # noqa: E402
import jd_trail  # noqa: E402

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
SQM_PER_ACRE = 4046.8564224

# Douglas-Peucker tolerance for the copy that gets drawn, in degrees of
# latitude: 0.00001 is about 1.1 m. Deere's boundaries average several
# hundred vertices a field and follow every creek bend; sampling every Nth
# point (the previous approach, 48 a ring) turned those bends into a
# staircase. At this tolerance roughly a third of the vertices survive and
# the shape is indistinguishable from the source at field zoom. Acreage is
# never computed from the simplified copy.
DP_TOLERANCE_DEG = 0.00001

# Six decimals is 0.11 m. Five (1.1 m) shows faint stepping on curves.
COORD_DECIMALS = 6

try:
    from pyproj import Geod
    _GEOD = Geod(ellps="WGS84")
except ImportError:                 # validation only; the map still builds
    _GEOD = None


def _acres(measurement: dict | None) -> float | None:
    """Deere reports area as valueAsDouble, usually in hectares.

    Full precision: Operations Center shows two decimals, and rounding each
    field before summing drifts a 200-field total by whole acres.
    """
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
        value /= SQM_PER_ACRE
    return value


def _signed_area(points: list[list[float]]) -> float:
    """Shoelace area in square degrees, on (lon, lat) so that the sign follows
    the shapefile convention: negative is clockwise, an outer ring."""
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        total += lon1 * lat2 - lon2 * lat1
    return total / 2.0


def _is_hole(ring: dict, points: list[list[float]]) -> bool:
    """Deere labels every ring exterior or interior. Trust that, and check it
    against orientation: a farmstead cutout notched in from the edge shares
    vertices with the outer ring, which is exactly where a point-in-polygon
    test goes wrong, so containment is never used to decide this."""
    kind = str(ring.get("type") or "").lower()
    if kind in ("interior", "exterior"):
        return kind == "interior"
    return _signed_area(points) > 0     # counter-clockwise


def douglas_peucker(points: list[list[float]], tol: float) -> list[list[float]]:
    """Simplify one closed ring, keeping its endpoints and its orientation.

    Longitude is scaled by cos(latitude) so the tolerance means the same
    distance east-west as north-south. Iterative rather than recursive:
    rings here reach 2,400 points.
    """
    n = len(points)
    if n < 5:
        return points
    scale = math.cos(math.radians(points[0][0]))
    xs = [p[1] * scale for p in points]
    ys = [p[0] for p in points]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay, bx, by = xs[a], ys[a], xs[b], ys[b]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        worst, worst_i = 0.0, -1
        for i in range(a + 1, b):
            if length == 0.0:
                d = math.hypot(xs[i] - ax, ys[i] - ay)
            else:
                d = abs(dy * xs[i] - dx * ys[i] + bx * ay - by * ax) / length
            if d > worst:
                worst, worst_i = d, i
        if worst > tol:
            keep[worst_i] = True
            stack.append((a, worst_i))
            stack.append((worst_i, b))
    out = [p for p, k in zip(points, keep) if k]
    # A closed ring needs at least a triangle plus its closing point. If the
    # simplifier ate a tiny ring, the original is the honest answer.
    return out if len(out) >= 4 else points


def geodesic_acres(rings: list[dict]) -> float | None:
    """Area on the WGS84 ellipsoid, holes subtracted. Validation only: the
    acreage the map shows is Deere's own figure, this is how it is checked."""
    if _GEOD is None:
        return None
    total = 0.0
    for ring in rings:
        pts = ring["p"]
        area, _ = _GEOD.polygon_area_perimeter([p[1] for p in pts], [p[0] for p in pts])
        total += -abs(area) if ring["t"] == "i" else abs(area)
    return total / SQM_PER_ACRE


def field_boundary(token: str, field: dict) -> dict:
    """Outline, centroid and acres for one field.

    Reads the full boundary, not Deere's "simplified" one: the two are within
    a handful of vertices of each other, and the full one is the record the
    acreage was computed from. Rings come back typed, in Deere's order, with
    the exterior clockwise and the holes counter-clockwise as the shapefile
    convention has it.
    """
    links = {l.get("rel"): l.get("uri") for l in field.get("links", [])}
    for rel in ("boundaries", "simplifiedBoundaries"):
        if rel not in links:
            continue
        status, body = api(token, links[rel])
        if status != 200 or not isinstance(body, dict):
            continue
        values = body.get("values") or [body]
        if not values:
            continue
        b = values[0]

        full, drawn = [], []
        orientation_disagreed = 0
        for poly in b.get("multipolygons") or []:
            for ring in poly.get("rings") or []:
                pts = [[p["lat"], p["lon"]] for p in ring.get("points") or []
                       if p.get("lat") is not None and p.get("lon") is not None]
                if len(pts) < 3:
                    continue
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                hole = _is_hole(ring, pts)
                if hole != (_signed_area(pts) > 0):
                    orientation_disagreed += 1
                kind = "i" if hole else "e"
                full.append({"t": kind, "p": pts})
                simplified = douglas_peucker(pts, DP_TOLERANCE_DEG)
                drawn.append({"t": kind, "p": [[round(p[0], COORD_DECIMALS),
                                                round(p[1], COORD_DECIMALS)]
                                               for p in simplified]})
        if not full:
            continue

        centroid = b.get("centroid") or {}
        area = _acres(b.get("area"))
        workable = _acres(b.get("workableArea"))
        return {
            "rings": drawn,
            "lat": centroid.get("lat"),
            "lon": centroid.get("lon"),
            # Deere's figures, carried through as the authority. Which one the
            # map shows is decided in the generator; both are kept so the
            # geodesic check below can say which one they match.
            "acres": workable if workable is not None else area,
            "acres_boundary": area,
            "acres_workable": workable,
            "acres_geodesic": geodesic_acres(full),
            "vertices": {"source": sum(len(r["p"]) for r in full),
                         "drawn": sum(len(r["p"]) for r in drawn)},
            "holes": sum(1 for r in full if r["t"] == "i"),
            "orientation_disagreed": orientation_disagreed,
            "detail": rel,
        }
    return {"rings": [], "lat": None, "lon": None, "acres": None, "detail": None}


# The machine records, which the ISO feed does not give. Note the path: the
# organization's own "machines" link points at /isg/equipment, NOT at
# /platform/organizations/{id}/machines - that one answers 403 and looks for
# all the world like a permissions problem. Always follow the link the API
# advertises rather than guessing a path from the docs.
EQUIPMENT = "https://api.deere.com/isg/equipment?organizationIds={org}"

# Position history, keyed by principalId from the equipment record - not by
# the equipment's own id, which 404s here.
LOCATION_HISTORY = ("https://api.deere.com/platform/machines/{pid}/locationHistory"
                    "?startDate={start}&endDate={end}&itemLimit=250")


def equipment_index(token: str, orgs: list[str]) -> dict:
    """Serial number -> machine record, across the given organizations.

    The ISO feed identifies a vehicle by serial number and the platform API
    by principalId; this is the join between them. All 37 road vehicles
    match on serial.
    """
    index: dict = {}
    for oid in orgs:
        status, body = api(token, EQUIPMENT.format(org=oid))
        if status != 200 or not isinstance(body, dict):
            continue
        for m in body.get("values", []):
            serial = str(m.get("serialNumber") or "").strip()
            if serial and m.get("principalId"):
                index[serial] = {"principal_id": m["principalId"],
                                 "name": m.get("name"), "org": oid}
    return index


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
            # Operating hours, when the tracker reports them. On these trucks
            # they are mostly 0.00 - they are aftermarket trackers, not Deere
            # machines with engine data - but where the figure does move it is
            # the only evidence of an engine actually running, which is what
            # separates idling from merely parked. None of the 37 road
            # vehicles carries CumulativeIdleHours at all.
            hours = eq.find("i:CumulativeOperatingHours", AEMP_NS)
            hour_val = hours.findtext("i:Hour", None, AEMP_NS) if hours is not None else None
            out.append({
                "name": (header.findtext("i:EquipmentID", "", AEMP_NS) or "").strip(),
                "make": make,
                "model": (header.findtext("i:Model", "", AEMP_NS) or "").strip(),
                "vin": (header.findtext("i:SerialNumber", "", AEMP_NS) or "").strip(),
                "kind": kind,
                "lat": float(lat) if lat else None,
                "lon": float(lon) if lon else None,
                "at": ts,
                "hours": float(hour_val) if hour_val else None,
                "hours_at": hours.get("datetime") if hours is not None else None,
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

    # How long each one has been stopped, from the running record kept by the
    # pusher. Reading it here too means a hand-built page carries the same
    # figures the live relay serves. The budget is larger than the pusher's
    # because this is run by hand, not every five minutes.
    jd_idle.track(out["trucks"])
    try:
        index = equipment_index(token, [str(o["id"]) for o in orgs])
        if jd_idle.refine(api, token, out["trucks"], index, budget=40):
            jd_idle.track(out["trucks"])
        jd_trail.update(api, token, out["trucks"], index, budget=40)
        out["stops"] = jd_trail.recent_events(24)
        out["stop_minutes"] = jd_trail.IDLE_MIN
    except Exception as exc:  # noqa: BLE001
        print(f"  (history/trail step skipped: {type(exc).__name__}: {exc})")

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

    # The acreage check. Every hole-handling mistake this pipeline could make
    # is invisible without it: two misclassified rings shift a 200-field total
    # by a tenth of a percent and look like rounding.
    checked = [f for f in out["fields"]
               if f.get("acres_geodesic") is not None and f.get("acres")]
    if checked:
        src = sum(f["vertices"]["source"] for f in checked)
        kept = sum(f["vertices"]["drawn"] for f in checked)
        holes = sum(f["holes"] for f in checked)
        flipped = sum(f["orientation_disagreed"] for f in checked)
        print(f"  vertices      {src:,} in Deere's boundaries, {kept:,} drawn "
              f"({100.0 * kept / src:.0f}%), {holes} holes"
              + (f", {flipped} rings whose orientation disagreed with their type"
                 if flipped else ""))
        for label, key in (("boundary", "acres_boundary"), ("workable", "acres_workable")):
            rows = [(f["acres_geodesic"], f[key]) for f in checked if f.get(key)]
            if not rows:
                continue
            geo, deere = sum(r[0] for r in rows), sum(r[1] for r in rows)
            errs = sorted(abs(g - d) / d * 100 for g, d in rows)
            print(f"  geodesic vs Deere {label:8s} {geo:12,.2f} vs {deere:12,.2f} ac  "
                  f"bias {100.0 * (geo - deere) / deere:+.4f}%  "
                  f"median field {errs[len(errs) // 2]:.4f}%  worst {errs[-1]:.3f}%")
        bad = [f for f in checked
               if abs(f["acres_geodesic"] - f["acres"]) / f["acres"] > 0.005]
        for f in bad:
            print(f"    CHECK {f['name']}: geodesic {f['acres_geodesic']:.2f} "
                  f"vs Deere {f['acres']:.2f} ac ({f['holes']} holes)")
    print(f"  semis         {len(semis):3d}  {freshest(semis)}")
    print(f"  pickups       {len(pickups):3d}  {freshest(pickups)}")
    trails = sum(1 for t in out["trucks"] if t.get("trail"))
    print(f"  trails        {trails:3d} vehicles with a path in the last "
          f"{jd_trail.TRAIL_HOURS} h, {len(out.get('stops') or [])} stop(s) "
          f"over {jd_trail.IDLE_MIN:.0f} min")
    if refused:
        print(f"  {refused} road vehicles had no position in the AEMP feed")


if __name__ == "__main__":
    main()
