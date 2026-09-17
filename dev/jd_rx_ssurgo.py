"""Soil survey productivity ratings under every field: CSR2 and NCCPI.

    python dev/jd_rx_ssurgo.py            fetch what is missing
    python dev/jd_rx_ssurgo.py --refresh  fetch everything again

Reads ~/.grain-map-secrets/fleet.json (dev/jd_fleet.py) and writes
~/.grain-map-secrets/ssurgo.json, which dev/jd_build_rx.py bakes into the
page. Everything comes from USDA-NRCS Soil Data Access, the public query
service over the SSURGO soil survey; no sign-in, no key.

For each field boundary the service returns the soil map-unit polygons
clipped to it. For each map unit it gives two ratings:

  CSR2   Iowa Corn Suitability Rating, 5-100, the number land is bought,
         rented and taxed on in Iowa. It is a column on the map unit
         itself (mapunit.iacornsr) and only Iowa map units carry one.
  NCCPI  National Commodity Crop Productivity Index, 0-1 in the survey and
         shown here 0-100, rated for every state. It is an interpretation
         held per soil component; a map unit's value is the average of its
         components weighted by how much of the unit each makes up, which
         is how Web Soil Survey aggregates it. Overall, corn and soybean
         sub-models are all kept.

The fetch is one spatial query per field plus a few batched lookups, and
is resumable: fields already in the file are skipped unless --refresh.
"""
from __future__ import annotations

import json
import math
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, now_iso, read_json, write_private  # noqa: E402
from jd_build_rx import field_geometry  # noqa: E402

FLEET = SECRETS / "fleet.json"
OUTPUT = SECRETS / "ssurgo.json"
SDA = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"
NCCPI_RULE = "NCCPI - National Commodity Crop Productivity Index (Ver 3.0)"
NCCPI_SUB = {
    NCCPI_RULE: "all",
    "NCCPI - NCCPI Corn Submodel (I)": "corn",
    "NCCPI - NCCPI Soybeans Submodel (I)": "soy",
}
# A slice of a map unit smaller than this is an edge sliver from the two
# boundaries not quite agreeing, not a soil.
MIN_PIECE_ACRES = 0.05
BATCH = 300


def sda(query: str, tries: int = 4) -> list:
    body = json.dumps({"query": query, "format": "JSON"}).encode()
    for attempt in range(tries):
        req = urllib.request.Request(SDA, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.load(r).get("Table", [])
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            detail = e.read().decode() if hasattr(e, "read") else str(e)
            if "<ServiceException>" in detail:
                detail = detail.split("<ServiceException>")[1].split("</ServiceException>")[0].strip()
            if getattr(e, "code", None) == 400 or attempt == tries - 1:
                raise RuntimeError(f"Soil Data Access failed: {detail}") from e
            time.sleep(3 * (attempt + 1))
    return []


# ---------------------------------------------------------------- geometry
def wkt_of(geom: dict) -> str:
    """GeoJSON Polygon/MultiPolygon (lon, lat) to WKT for SQL Server."""
    def ring(r):
        pts = list(r)
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        return "(" + ",".join(f"{x:.6f} {y:.6f}" for x, y in pts) + ")"

    def poly(rings):
        return "(" + ",".join(ring(r) for r in rings) + ")"

    if geom["type"] == "Polygon":
        return "POLYGON" + poly(geom["coordinates"])
    return "MULTIPOLYGON(" + ",".join(poly(p) for p in geom["coordinates"]) + ")"


def parse_wkt_polygons(text: str) -> list[list[list[list[float]]]]:
    """Every polygon in a WKT geometry, as [exterior, hole, ...] rings of
    (lat, lon). The clip of two nearly-coincident edges can come back as a
    GEOMETRYCOLLECTION with stray lines and points in it; only the
    polygons matter."""
    out = []
    for m in re.finditer(r"(?<![A-Z])(MULTIPOLYGON|POLYGON)\s*\(", text):
        block, _ = _paren_block(text, m.end() - 1)
        nested = _nest(block)
        polys = nested if m.group(1) == "MULTIPOLYGON" else [nested]
        for rings in polys:
            out.append([[[round(y, 6), round(x, 6)] for x, y in ring] for ring in rings])
    return out


def _paren_block(text: str, start: int) -> tuple[str, int]:
    """The text from the '(' at start to its matching ')', inclusive."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
    raise ValueError("unbalanced WKT")


def _nest(block: str):
    """'(...)' to nested lists; the innermost level is a list of (x, y)."""
    inner = block[1:-1].strip()
    if not inner.startswith("("):
        return [tuple(float(v) for v in pair.split()[:2]) for pair in inner.split(",")]
    items, i = [], 0
    while i < len(inner):
        if inner[i] == "(":
            sub, i = _paren_block(inner, i)
            items.append(_nest(sub))
        else:
            i += 1
    return items


def ring_acres(ring: list[list[float]]) -> float:
    """Shoelace on a lat/lon ring, metres via the local cosine."""
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[0] for p in ring) / len(ring)
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    a = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i][1] * kx, ring[i][0] * ky
        x1, y1 = ring[i + 1][1] * kx, ring[i + 1][0] * ky
        a += x0 * y1 - x1 * y0
    return abs(a) / 2 / 4046.856


def poly_acres(rings: list) -> float:
    return ring_acres(rings[0]) - sum(ring_acres(h) for h in rings[1:])


# ------------------------------------------------------------------ fetch
def field_pieces(geom: dict) -> list[dict]:
    wkt = wkt_of(geom).replace("'", "")
    # The service refuses DECLARE, so the boundary is written out twice.
    # MakeValid: Deere boundaries can have a hole that touches or crosses
    # its own edge, and SQL Server returns nothing at all for those.
    g = f"geometry::STGeomFromText('{wkt}', 4326).MakeValid()"
    rows = sda(
        f"SELECT mp.mukey, mp.mupolygongeo.STIntersection({g}).STAsText() "
        f"FROM mupolygon mp WHERE mp.mupolygongeo.STIntersects({g}) = 1")
    pieces = []
    for mukey, text in rows:
        for rings in parse_wkt_polygons(text or ""):
            ac = poly_acres(rings)
            if ac >= MIN_PIECE_ACRES:
                pieces.append({"mukey": str(mukey), "rings": rings, "ac": round(ac, 3)})
    return pieces


def mapunit_ratings(mukeys: list[str]) -> dict:
    out: dict = {}
    for i in range(0, len(mukeys), BATCH):
        keys = ",".join(mukeys[i:i + BATCH])
        for mukey, musym, muname, csr, farm, area in sda(
                "SELECT m.mukey, m.musym, m.muname, m.iacornsr, m.farmlndcl, l.areasymbol "
                f"FROM mapunit m JOIN legend l ON l.lkey = m.lkey WHERE m.mukey IN ({keys})"):
            # CSR2 is an Iowa rating; a Missouri survey area can carry a
            # stray value in the column and it means nothing there.
            iowa = str(area or "").upper().startswith("IA")
            out[str(mukey)] = {
                "musym": musym, "muname": muname, "area": area,
                "csr2": int(csr) if iowa and csr not in (None, "") else None,
                "farmland": farm, "nccpi": {},
            }
        rules = ",".join(f"'{r}'" for r in NCCPI_SUB)
        acc: dict = {}
        for mukey, pct, rule, val in sda(
                "SELECT c.mukey, c.comppct_r, ci.rulename, ci.interphr "
                "FROM component c JOIN cointerp ci ON ci.cokey = c.cokey "
                f"WHERE c.mukey IN ({keys}) AND ci.mrulename = '{NCCPI_RULE}' "
                f"AND ci.rulename IN ({rules}) AND ci.interphr IS NOT NULL"):
            if pct in (None, ""):
                continue
            slot = acc.setdefault(str(mukey), {}).setdefault(NCCPI_SUB[rule], [0.0, 0.0])
            slot[0] += float(val) * float(pct)
            slot[1] += float(pct)
        for mukey, subs in acc.items():
            if mukey in out:
                out[mukey]["nccpi"] = {k: round(num / den * 100, 1)
                                       for k, (num, den) in subs.items() if den}
    return out


def main() -> None:
    refresh = "--refresh" in sys.argv
    fleet = read_json(FLEET, None)
    if not fleet:
        sys.exit(f"no {FLEET} - run dev/jd_fleet.py first")
    store = {} if refresh else read_json(OUTPUT, {})
    fields_out: dict = store.get("fields", {})
    units: dict = store.get("mapunits", {})

    fields = [f for f in fleet["fields"] if f.get("rings")]
    todo = [f for f in fields if f["name"] not in fields_out]
    print(f"{len(fields)} fields, {len(todo)} to fetch")
    t0 = time.time()
    for i, f in enumerate(todo, 1):
        geom = field_geometry(f)
        if not geom:
            continue
        try:
            pieces = field_pieces(geom)
        except RuntimeError as e:
            print(f"  {f['name']}: {e}")
            continue
        fields_out[f["name"]] = {"id": f["id"], "pieces": pieces}
        if i % 10 == 0 or i == len(todo):
            write_private(OUTPUT, {"generated_at": now_iso(), "fields": fields_out, "mapunits": units})
            print(f"  {i}/{len(todo)}  {time.time() - t0:.0f}s")

    need = sorted({p["mukey"] for rec in fields_out.values() for p in rec["pieces"]} - set(units))
    if need or refresh:
        print(f"ratings for {len(need)} map units")
        units.update(mapunit_ratings(need))
    write_private(OUTPUT, {"generated_at": now_iso(), "fields": fields_out, "mapunits": units})

    rated_csr = sum(1 for u in units.values() if u["csr2"] is not None)
    rated_n = sum(1 for u in units.values() if u["nccpi"].get("all") is not None)
    print(f"wrote {OUTPUT}: {len(fields_out)} fields, {len(units)} map units, "
          f"CSR2 on {rated_csr}, NCCPI on {rated_n}")


if __name__ == "__main__":
    main()
