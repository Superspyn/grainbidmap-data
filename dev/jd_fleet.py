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


def machine_position(token: str, machine_id: str):
    """Last known position for one machine, or None.

    Deere exposes this under the Machines API. The equipment listing endpoint
    (/isg/equipment) does not carry positions, so this is a second call per
    machine - which is why the result is cached to disk rather than fetched
    per page view.
    """
    for path in (f"/platform/machines/{machine_id}/locations?lastKnown=true",
                 f"/platform/machines/{machine_id}/locationHistory?lastKnown=true"):
        status, body = api(token, path)
        if status == 200 and isinstance(body, dict):
            values = body.get("values") or []
            if values:
                v = values[0]
                point = v.get("point") or {}
                return {
                    "lat": point.get("lat"),
                    "lon": point.get("lon"),
                    "timestamp": v.get("timestamp") or v.get("eventTimestamp"),
                }
    return None


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

        for m in api_all(token, f"/isg/equipment?organizationIds={oid}"):
            kind = ((m.get("isgType") or {}).get("name")
                    or (m.get("type") or {}).get("name") or "")
            if kind.strip().lower() not in TRUCK_TYPES:
                continue
            entry = {
                "org": oid,
                "id": str(m.get("id")),
                "name": m.get("name"),
                "make": (m.get("make") or {}).get("name"),
                "vin": m.get("serialNumber"),
                "telematics": bool(m.get("telematicsCapable")),
                "position": None,
            }
            if entry["telematics"]:
                pos = machine_position(token, entry["id"])
                if pos:
                    entry["position"] = pos
                else:
                    refused += 1
            out["trucks"].append(entry)

    OUTPUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    try:
        os.chmod(OUTPUT, 0o600)
    except OSError:
        pass

    located = sum(1 for t in out["trucks"] if t["position"])
    outlined = sum(1 for f in out["fields"] if f.get("rings"))
    print(f"wrote {OUTPUT}")
    print(f"  organizations {len(out['organizations'])}")
    print(f"  fields        {len(out['fields'])}  ({outlined} with a boundary)")
    print(f"  trucks        {len(out['trucks'])}  ({located} with a position)")
    if refused:
        print(f"  {refused} telematics-capable trucks returned no position.")
        print("  If that is all of them, the app is probably missing the")
        print('  "Operations Center - Machines" API - request it on your app')
        print("  at developer.deere.com, Access tab.")


if __name__ == "__main__":
    main()
