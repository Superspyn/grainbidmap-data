"""Build the private prescription page from Deere data and the soil tests.

    python dev/jd_build_rx.py

Reads, all in ~/.grain-map-secrets/:
    fleet.json          fields and boundaries (dev/jd_fleet.py)
    soil-samples.json   lab grids (dev/jd_rx_soil.py)
    rx-ops.json         crop, harvest and application history (dev/jd_rx_pull.py)
    rx-yield/*.json     thinned yield maps (dev/jd_rx_pull.py)

and writes ~/.grain-map-secrets/rx-builder.html: the code in rx-builder.html
at the repo root with the data baked in. Open it in a browser from disk.
Output goes outside the repo on purpose - the repo is public and the field
data is not.

Soil grids are matched to Deere fields by WHERE THE SAMPLES ARE, not by
name. Field names carry the acreage and get renamed when a boundary is
redrawn (Robards190Warn10 became Robards230Warn10), and 20 of the 213
sampled fields no longer exist under the name the lab knew them by. A grid
belongs to whichever boundary most of its points fall inside.
"""
from __future__ import annotations

import glob
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, now_iso, read_json  # noqa: E402

FLEET = SECRETS / "fleet.json"
SOIL = SECRETS / "soil-samples.json"
OPS = SECRETS / "rx-ops.json"
YIELD_DIR = SECRETS / "rx-yield"
OUTPUT = SECRETS / "rx-builder.html"
SOURCE = pathlib.Path(__file__).resolve().parent.parent / "rx-builder.html"
MARKER = "// ====== Field data (baked in by dev/jd_build_rx.py) ======"

PLAN_YEAR = 2027
ORG_SHORT = {"5294": "North", "567678": "South", "1190261": "Trucking"}


# ---------------------------------------------------------------- geometry

def pip(x: float, y: float, ring: list[list[float]]) -> bool:
    """Point in polygon on a (lon, lat) ring, even-odd rule."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def bbox(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def field_geometry(field: dict) -> dict | None:
    """GeoJSON Polygon or MultiPolygon, holes attached to the part around them.

    fleet.json rings are (lat, lon) with a type letter; GeoJSON wants
    (lon, lat), exterior first, holes after. Deere's own ring labels decide
    what is a hole - not the winding, which Deere does not keep consistent.
    """
    rings = field.get("rings") or []
    exts = [[[round(p[1], 6), round(p[0], 6)] for p in r["p"]]
            for r in rings if r["t"] == "e"]
    holes = [[[round(p[1], 6), round(p[0], 6)] for p in r["p"]]
             for r in rings if r["t"] != "e"]
    if not exts:
        return None
    parts = [[e] for e in exts]
    boxes = [bbox(e) for e in exts]
    for h in holes:
        hx, hy = h[0]
        home = None
        for i, e in enumerate(exts):
            x0, y0, x1, y1 = boxes[i]
            if x0 <= hx <= x1 and y0 <= hy <= y1 and pip(hx, hy, e):
                home = i
                break
        if home is None:
            # A hole vertex can sit exactly on the outer edge; try its middle.
            mid = h[len(h) // 2]
            for i, e in enumerate(exts):
                if pip(mid[0], mid[1], e):
                    home = i
                    break
        if home is not None:
            parts[home].append(h)
    if len(parts) == 1:
        return {"type": "Polygon", "coordinates": parts[0]}
    return {"type": "MultiPolygon", "coordinates": parts}


def contains(geom: dict, x: float, y: float) -> bool:
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    for rings in polys:
        if pip(x, y, rings[0]) and not any(pip(x, y, h) for h in rings[1:]):
            return True
    return False


# ---------------------------------------------------------------- matching

def match_soil(soil: dict, fields: list[dict], geoms: dict) -> tuple[dict, list]:
    """Soil grids re-keyed by the Deere field they sit in, plus a report."""
    boxes = {}
    for f in fields:
        g = geoms.get(f["name"])
        if not g:
            continue
        rings = [g["coordinates"][0]] if g["type"] == "Polygon" else [p[0] for p in g["coordinates"]]
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        boxes[f["name"]] = (min(xs), min(ys), max(xs), max(ys))
    by_name = {f["name"] for f in fields}
    out: dict = {}
    report = []
    for soil_name, rec in soil.items():
        pts = []
        for s in reversed(rec["sets"]):
            if s["pts"]:
                pts = s["pts"]
                break
        best, best_n = None, 0
        if pts:
            counts: dict[str, int] = {}
            for p in pts:
                x, y = p["x"], p["y"]
                for name, (x0, y0, x1, y1) in boxes.items():
                    if x0 <= x <= x1 and y0 <= y <= y1 and contains(geoms[name], x, y):
                        counts[name] = counts.get(name, 0) + 1
            if counts:
                best, best_n = max(counts.items(), key=lambda kv: kv[1])
        if best and best_n >= max(3, len(pts) * 0.5):
            how = "same name" if best == soil_name else f"by location ({best_n}/{len(pts)} pts)"
        elif soil_name in by_name:
            best, how = soil_name, "by name only (points do not fall in it!)"
        else:
            report.append((soil_name, None, f"unmatched ({best_n}/{len(pts)} pts in {best})"))
            continue
        if best in out:
            # Two grids landed in one boundary (a field that was split or
            # merged): keep every sampling event, oldest first.
            out[best]["sets"] = sorted(out[best]["sets"] + rec["sets"], key=lambda s: s["d"])
            how += " (merged with another grid)"
        else:
            out[best] = {"county": rec.get("county"), "sets": rec["sets"]}
        report.append((soil_name, best, how))
    return out, report


# ---------------------------------------------------------------- history

def condense_ops(rec: dict) -> dict:
    """One entry per season: what went in, what came off, what was applied."""
    seasons: dict = {}
    for op in rec.get("ops", []):
        yr = op.get("season")
        if not yr:
            continue
        s = seasons.setdefault(str(yr), {"apps": []})
        if op["type"] == "seeding" and op.get("crop"):
            if not s.get("crop") or (op.get("start") or "") >= (s.get("planted") or ""):
                s["crop"] = op["crop"]
                s["planted"] = (op.get("start") or "")[:10]
                s["varieties"] = op.get("varieties") or []
        elif op["type"] == "harvest":
            h = {"crop": op.get("crop"), "d": (op.get("start") or "")[:10]}
            for k in ("avg", "acres", "moisture"):
                if op.get(k) is not None:
                    h[k] = op[k]
            # A field harvested in two passes: keep the bigger one's numbers,
            # but note both dates.
            if "harvest" in s and (s["harvest"].get("acres") or 0) >= (h.get("acres") or 0):
                continue
            s["harvest"] = h
        elif op["type"] == "application":
            prods = [p for p in op.get("products", []) if p.get("name") and p["name"] != "---"]
            if prods:
                s["apps"].append({"d": (op.get("start") or "")[:10], "products": prods})
    for s in seasons.values():
        if not s.get("crop") and s.get("harvest", {}).get("crop"):
            s["crop"] = s["harvest"]["crop"]
            s["crop_from_harvest"] = True
    return {"seasons": seasons}


# ---------------------------------------------------------------- main

def js_const(name: str, obj) -> str:
    return f"const {name}={json.dumps(obj, separators=(',', ':'), ensure_ascii=False)};"


def main() -> None:
    fleet = read_json(FLEET, None)
    if not fleet:
        sys.exit(f"no {FLEET} - run dev/jd_fleet.py first")
    soil = read_json(SOIL, {}).get("fields", {})
    ops_file = read_json(OPS, {})
    ops = ops_file.get("fields", {})
    county = ops_file.get("county", {})
    fields = [f for f in fleet["fields"] if f.get("rings")]

    geoms = {}
    features = []
    for f in fields:
        g = field_geometry(f)
        if not g:
            continue
        geoms[f["name"]] = g
        features.append({"type": "Feature", "properties": {
            "field": f["name"], "farm": _county(f, county, soil),
            "org": ORG_SHORT.get(str(f["org"]), str(f["org"])), "id": f["id"],
            "acres": round(f.get("acres_workable") or f.get("acres") or 0, 2),
            "crop": f.get("crop"), "crop_season": f.get("crop_season"),
        }, "geometry": g})

    soil_by_field, report = match_soil(soil, fields, geoms)
    ops_by_field = {}
    for rec in ops.values():
        ops_by_field[rec["name"]] = condense_ops(rec)

    ymaps: dict = {}
    for path in glob.glob(str(YIELD_DIR / "*.json")):
        m = read_json(pathlib.Path(path), None)
        if not m:
            continue
        ymaps.setdefault(m["field"], []).append({k: m[k] for k in (
            "season", "crop", "avg", "acres", "moisture", "lon0", "lat0",
            "dlon", "dlat", "nx", "ny", "idx") if k in m})
    for maps in ymaps.values():
        maps.sort(key=lambda m: m["season"])

    meta = {"generated": now_iso(), "plan_year": PLAN_YEAR,
            "fields": len(features), "sampled": len(soil_by_field),
            "with_history": len(ops_by_field), "with_yield_maps": len(ymaps)}
    data = "\n".join([
        js_const("META", meta),
        js_const("B", {"type": "FeatureCollection", "features": features}),
        js_const("SOIL", soil_by_field),
        js_const("OPS", ops_by_field),
        js_const("YMAPS", ymaps),
    ])

    html = SOURCE.read_text(encoding="utf-8")
    if MARKER not in html:
        sys.exit(f"marker not found in {SOURCE}")
    html = html.replace(MARKER, MARKER + "\n" + data, 1)
    OUTPUT.write_text(html, encoding="utf-8", newline="\n")

    for soil_name, deere_name, how in sorted(report):
        if deere_name != soil_name:
            print(f"  soil {soil_name!r} -> {deere_name!r}: {how}")
    print(f"fields {len(features)}, soil grids matched {len(soil_by_field)} of {len(soil)}, "
          f"history for {len(ops_by_field)}, yield maps for {len(ymaps)} fields "
          f"({sum(len(v) for v in ymaps.values())} harvests)")
    print(f"wrote {OUTPUT}  ({OUTPUT.stat().st_size / 1e6:.1f} MB)")


def _county(field: dict, county: dict, soil: dict) -> str:
    """Deere calls the county a 'farm'. From the pull's field->farm table,
    else from the lab's record of the field, else unknown. Missouri farms
    come as 'Macon_MO', 'Daviess-Mo', 'Mercer-MO'; tidy that to one form."""
    name = county.get(field["id"]) or (soil.get(field["name"]) or {}).get("county") or "Unknown"
    name = name.strip()
    for suffix in ("_MO", "-MO", "-Mo", " MO"):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip() + ", MO"
    return name


if __name__ == "__main__":
    main()
