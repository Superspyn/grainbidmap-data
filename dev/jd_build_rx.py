"""Build the private prescription page from Deere data and the soil tests.

    python dev/jd_build_rx.py

Reads, all in ~/.grain-map-secrets/:
    fleet.json          fields and boundaries (dev/jd_fleet.py)
    soil-samples.json   lab grids and composites (dev/jd_rx_soil_import.py)
    rx-ops.json         crop, harvest and application history (dev/jd_rx_pull.py)
    rx-yield/*.json     thinned yield maps (dev/jd_rx_pull.py)

and writes ~/.grain-map-secrets/rx-builder.html: the code in rx-builder.html
at the repo root with the data baked in. Open it in a browser from disk.
Output goes outside the repo on purpose - the repo is public and the field
data is not.

Soil grids are matched to Deere fields by WHERE THE SAMPLES ARE, not by
name. Field names carry the acreage and get renamed when a boundary is
redrawn (Rvrsde190Warn10 became Rvrsde230Warn10), and 20 of the 213
sampled fields no longer exist under the name the lab knew them by. A grid
belongs to whichever boundary most of its points fall inside.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import math
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
        if not pts and soil_name in by_name:
            # A whole-field composite: no coordinates to match on, but the
            # importer already resolved the lab's name to this field.
            out.setdefault(soil_name, {"county": rec.get("county"), "sets": []})
            out[soil_name]["sets"] = sorted(out[soil_name]["sets"] + rec["sets"],
                                            key=lambda s: s["d"])
            report.append((soil_name, soil_name, "whole-field composite"))
            continue
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


# --------------------------------------------- grid pattern, newest level

def averages(pts: list[dict]) -> dict:
    """Field average per nutrient over the points that reported it."""
    tot: dict = {}
    for p in pts:
        for k, v in p.items():
            if k in ("x", "y", "id") or not isinstance(v, (int, float)):
                continue
            a, n = tot.get(k, (0.0, 0))
            tot[k] = (a + v, n + 1)
    return {k: round(a / n, 3) for k, (a, n) in tot.items() if n}


# pH is a logarithm, so a 2026 pH of 6.4 against a 2022 average of 6.0 is a
# shift of +0.4 across the field, not a multiplication by 1.067. Everything
# else here is a concentration and moves by ratio.
SHIFT_KEYS = ("ph", "bph")
PH_RANGE = (3.5, 9.5)
# Nothing is anchored on an average built from a handful of readings, or on
# a move so large it is more likely a different field than a real change.
MIN_RATIO, MAX_RATIO = 0.25, 4.0


def scale_grid_to_whole(rec: dict) -> dict | None:
    """A derived sampling event: the newest grid's pattern, moved to the
    level of a newer whole-field composite.

    A field with a 2022 grid and a 2026 composite knows two different
    things. The grid knows where the good ground is; the composite knows
    what the field tests today. Neither alone makes a good prescription,
    so this holds the grid's shape and slides it onto the composite's
    level, nutrient by nutrient: every point moves by the same ratio its
    field average moved.

    It is a derived reading, not a lab result, and is labelled as one
    everywhere it appears. A nutrient the composite did not report keeps
    its grid value, since a four-year-old measurement beats none, and the
    set records which nutrients were actually anchored."""
    grids = [s for s in rec["sets"] if s.get("kind") != "whole" and s.get("pts")]
    wholes = [s for s in rec["sets"] if s.get("kind") == "whole"]
    if not grids or not wholes:
        return None
    grid, whole = grids[-1], wholes[-1]
    if whole["d"] <= grid["d"]:
        return None

    gavg, wavg = grid.get("avg") or {}, whole.get("avg") or {}
    moves: dict = {}
    for key, wv in wavg.items():
        gv = gavg.get(key)
        if gv is None or wv is None:
            continue
        if key in SHIFT_KEYS:
            moves[key] = ("shift", round(wv - gv, 3))
        elif gv > 0 and MIN_RATIO <= wv / gv <= MAX_RATIO:
            moves[key] = ("ratio", round(wv / gv, 5))
    if not moves:
        return None

    pts = []
    for p in grid["pts"]:
        q = {"x": p["x"], "y": p["y"], "id": p.get("id")}
        for key, v in p.items():
            if key in ("x", "y", "id") or not isinstance(v, (int, float)):
                continue
            mv = moves.get(key)
            if not mv:
                q[key] = v                      # untouched: kept from the grid
            elif mv[0] == "shift":
                q[key] = round(min(PH_RANGE[1], max(PH_RANGE[0], v + mv[1])), 2)
            else:
                q[key] = round(max(0.0, v * mv[1]), 3)
        pts.append(q)
    if not pts:
        return None

    return {
        "d": whole["d"], "n": len(pts), "lab": whole.get("lab"),
        "kind": "scaled", "pts": pts, "avg": averages(pts),
        "from_grid": grid["d"], "from_whole": whole["d"],
        "report": whole.get("report"),
        "anchored": sorted(moves),
        "moves": {k: v[1] for k, v in sorted(moves.items())},
    }


def add_scaled_sets(soil_by_field: dict) -> int:
    n = 0
    for rec in soil_by_field.values():
        s = scale_grid_to_whole(rec)
        if s:
            rec["sets"].append(s)
            n += 1
    return n


# ------------------------------------------------------- whole-field spread

# One pseudo-sample per this many acres, so a composite field gets a grid
# of the same order as a real 2.5-acre grid.
SPREAD_ACRES = 2.5
MAX_SPREAD = 400


# Two whole-field reports this far apart in days can still be one sampling
# sent to the lab in two batches. Beyond it, the later one is a re-test and
# supersedes.
SPLIT_DAYS = 150


def merge_split_wholes(soil_by_field: dict) -> list:
    """Merge whole-field composites that are two halves of one sampling.

    Boyd445GrantE15,14,13 was sampled once on a 2.5-acre grid and billed as
    two reports: 136 samples in December and 46 in March. Each became its
    own whole-field composite covering the entire 445 acres, and the page
    read the field through the newer, smaller one - phosphorus 16.8 instead
    of the 22.2 the 182 samples together give, which is the difference
    between 162 lb of MAP an acre and none at all.

    Two composites inside one sampling window are averaged into one,
    weighted by how many samples each holds. A composite that already
    matches a grid has been dropped by then, so this only ever sees
    coordinate-less reports."""
    merged = []
    for name, rec in soil_by_field.items():
        wholes = [s for s in rec["sets"] if s.get("kind") == "whole"]
        if len(wholes) < 2:
            continue
        groups, used = [], set()
        for i, a in enumerate(wholes):
            if i in used:
                continue
            group = [a]
            used.add(i)
            for j in range(i + 1, len(wholes)):
                b = wholes[j]
                if j in used:
                    continue
                gap = abs((dt.date.fromisoformat(b["d"])
                           - dt.date.fromisoformat(a["d"])).days)
                if gap <= SPLIT_DAYS:
                    group.append(b)
                    used.add(j)
            if len(group) > 1:
                groups.append(group)
        for group in groups:
            ns = [s.get("n") or 1 for s in group]
            total = sum(ns)
            avg: dict = {}
            for key in {k for s in group for k in s["avg"]}:
                num = den = 0.0
                for s, n in zip(group, ns):
                    if s["avg"].get(key) is not None:
                        num += s["avg"][key] * n
                        den += n
                if den:
                    avg[key] = round(num / den, 3)
            keep = max(group, key=lambda s: s.get("n") or 0)
            combined = dict(keep)
            combined.update({
                "d": max(s["d"] for s in group), "n": total, "avg": avg,
                "kind": "whole",
                "report": ", ".join(str(s.get("report") or "") for s in group).strip(", "),
                "split_reports": [s.get("report") for s in group],
                "split_counts": ns,
            })
            for s in group:
                rec["sets"].remove(s)
            rec["sets"].append(combined)
            rec["sets"].sort(key=lambda s: (s["d"], s.get("kind") == "scaled"))
            merged.append((name, [s.get("report") for s in group], ns))
    return merged


def drop_duplicate_wholes(soil_by_field: dict) -> int:
    """A whole-field row that is the average of a grid event this field
    already has is dropped, keeping the points.

    The importer does this too, but it can only compare records under the
    same lab name. The composite carries the Operations Center name while
    the grid carries the lab's ("Lakeview473Hrdn3132" vs
    "Lakeview473Hrdn31,32"), so the pair only meets here, after matching."""
    n = 0
    for rec in soil_by_field.values():
        grids = [dt.date.fromisoformat(s["d"]) for s in rec["sets"]
                 if s.get("kind") != "whole"]
        keep = []
        for s in rec["sets"]:
            if s.get("kind") == "whole":
                d = dt.date.fromisoformat(s["d"])
                if any(abs((d - g).days) <= 14 for g in grids):
                    n += 1
                    continue
            keep.append(s)
        rec["sets"] = keep
    return n


def spread_whole(rec: dict, geom: dict) -> int:
    """Give every whole-field composite a point set covering its boundary.

    A 2026 composite is one row of lab numbers for the whole field, with
    no coordinates. The page draws rates from points, so the composite is
    laid out as an even grid of identical samples inside the boundary.
    The nutrient map is then flat, which is honest - one lab number is
    all we know - but the RATE map still varies across the field, because
    removal is scaled by the yield index at each spot. The page marks
    these fields as composites so nobody reads the flat map as a grid."""
    made = 0
    for s in rec["sets"]:
        if s.get("kind") != "whole" or s.get("pts"):
            continue
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        xs = [c[0] for rings in polys for c in rings[0]]
        ys = [c[1] for rings in polys for c in rings[0]]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        lat = (y0 + y1) / 2
        side = math.sqrt(SPREAD_ACRES * 4046.8564)          # metres
        dlat = side / 111320.0
        dlon = dlat / max(0.2, math.cos(math.radians(lat)))
        pts = []
        y = y0 + dlat / 2
        while y < y1 and len(pts) < MAX_SPREAD:
            x = x0 + dlon / 2
            while x < x1 and len(pts) < MAX_SPREAD:
                if contains(geom, x, y):
                    pts.append({"x": round(x, 7), "y": round(y, 7),
                                "id": len(pts) + 1, **s["avg"]})
                x += dlon
            y += dlat
        if not pts:                      # field smaller than one cell
            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)
            pts = [{"x": round(cx, 7), "y": round(cy, 7), "id": 1, **s["avg"]}]
        s["pts"] = pts
        s["spread"] = len(pts)
        made += 1
    return made


# ---------------------------------------------------------------- history

def condense_ops(rec: dict) -> dict:
    """One entry per season: what went in, what came off, what was applied.

    The season's crop is what was HARVESTED, when there is a harvest worth
    the name. Deere gives a fall-seeded cover crop the same crop season as
    the cash crop that came off weeks earlier, and taking the latest
    seeding made 79 seasons read "oats" on ground that grew and harvested
    soybeans or corn - which flips the rotation the plan year is built
    from and drops the 45 lb soybean nitrogen credit. The cover crop is
    kept separately, because knowing it went on is worth something.

    With no harvest to go on, a seeding after the cover-crop cutoff is
    only used if there is nothing earlier in the season."""
    seasons: dict = {}
    for op in rec.get("ops", []):
        yr = op.get("season")
        if not yr:
            continue
        s = seasons.setdefault(str(yr), {"apps": [], "seedings": []})
        if op["type"] == "seeding" and op.get("crop"):
            s["seedings"].append({
                "crop": op["crop"], "d": (op.get("start") or "")[:10],
                "varieties": op.get("varieties") or [],
                "acres": op.get("acres"),
            })
        elif op["type"] == "harvest":
            h = {"crop": op.get("crop"), "d": (op.get("start") or "")[:10]}
            for k in ("avg", "acres", "moisture"):
                if op.get(k) is not None:
                    h[k] = op[k]
            s.setdefault("harvests", []).append(h)
        elif op["type"] == "application":
            prods = [p for p in op.get("products", []) if p.get("name") and p["name"] != "---"]
            if prods:
                s["apps"].append({"d": (op.get("start") or "")[:10], "products": prods})

    for yr, s in seasons.items():
        seedings = sorted(s.pop("seedings", []), key=lambda x: x["d"])
        harvests = sorted(s.pop("harvests", []), key=lambda x: x["d"])
        s["harvest"] = combine_harvests(harvests)
        if harvests:
            s["harvests"] = harvests

        main = None
        big = [h for h in harvests if (h.get("acres") or 0) >= MIN_HARVEST_ACRES]
        if big:
            # Grown and taken off: that is the season's crop, whatever was
            # drilled over the stubble afterwards.
            want = crop_family(big[-1]["crop"])
            match = [x for x in seedings if crop_family(x["crop"]) == want]
            main = match[-1] if match else None
            s["crop"] = big[-1]["crop"]
            if not match:
                s["crop_from_harvest"] = True
        if main is None:
            early = [x for x in seedings if x["d"][5:] <= COVER_CUTOFF]
            main = (early or seedings)[-1] if seedings else None
            if main and not s.get("crop"):
                s["crop"] = main["crop"]
        if main:
            s["planted"] = main["d"]
            s["varieties"] = main["varieties"]
        cover = [x for x in seedings if x is not main and x["d"][5:] > COVER_CUTOFF]
        if cover:
            s["cover"] = [{"crop": x["crop"], "d": x["d"]} for x in cover]
    return {"seasons": seasons}


# A seeding after this day of the year, on ground that already grew a cash
# crop that season, is a cover crop rather than the season's crop.
COVER_CUTOFF = "08-01"
# A harvest smaller than this is a partial pass, not evidence of the crop.
MIN_HARVEST_ACRES = 5


def crop_family(name) -> str:
    n = str(name or "").lower()
    if "corn" in n:
        return "corn"
    if "soy" in n or "bean" in n:
        return "soybean"
    return n


def combine_harvests(harvests: list) -> dict:
    """One number for a season harvested in more than one pass.

    Keeping only the biggest pass threw the rest away: Boyd445 took 306
    acres at 178.7 bu in September and 130 acres at 127.0 bu the following
    February, and recorded 178.7 - which then set a yield goal 9% high on
    the whole field. Same-crop passes are averaged over their acres."""
    if not harvests:
        return {}
    want = crop_family(harvests[-1]["crop"])
    same = [h for h in harvests if crop_family(h["crop"]) == want]
    if not same:
        same = harvests
    out = dict(same[-1])
    acres = [h.get("acres") or 0 for h in same]
    total = sum(acres)
    if len(same) > 1 and total > 0:
        num = sum((h.get("avg") or 0) * (h.get("acres") or 0) for h in same)
        wet = sum((h.get("moisture") or 0) * (h.get("acres") or 0) for h in same)
        out["avg"] = round(num / total, 1)
        out["acres"] = round(total, 2)
        if any(h.get("moisture") is not None for h in same):
            out["moisture"] = round(wet / total, 1)
        out["passes"] = [{"d": h["d"], "acres": h.get("acres"), "avg": h.get("avg")}
                         for h in same]
        out["d"] = same[0]["d"]
    return out


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
    dupes = drop_duplicate_wholes(soil_by_field)
    split = merge_split_wholes(soil_by_field)
    scaled = add_scaled_sets(soil_by_field)
    spread = sum(spread_whole(rec, geoms[name])
                 for name, rec in soil_by_field.items() if name in geoms)
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
    if dupes:
        print(f"  {dupes} whole-field rows dropped as the average of a grid "
              f"the same field already has")
    for name, reports, counts in split:
        print(f"  {name}: one sampling billed as {len(reports)} reports "
              f"({'+'.join(str(c) for c in counts)} samples) merged into one")
    if scaled:
        print(f"  {scaled} fields given the newest whole-field level on their "
              f"grid's pattern")
    if spread:
        print(f"  {spread} whole-field composites spread over their boundaries")
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
        if name.upper().endswith(suffix.upper()):
            # One suffix only: tidying "Mercer-MO" to "Mercer, MO" would
            # otherwise match " MO" on the next pass and give "Mercer,, MO".
            return name[: -len(suffix)].strip(" ,") + ", MO"
    return name


if __name__ == "__main__":
    main()
