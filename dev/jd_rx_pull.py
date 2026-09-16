"""Pull what the prescription builder needs from Operations Center.

    python dev/jd_rx_pull.py                   ops history + yield maps, 3 newest seasons
    python dev/jd_rx_pull.py --seasons 2022 2023 2024 2025
    python dev/jd_rx_pull.py --no-yield        just the operation history (minutes)
    python dev/jd_rx_pull.py --field Lakeview171Mdsn6 --field Rvrsde53Brtt27

Reads ~/.grain-map-secrets/fleet.json (run dev/jd_fleet.py first for the
fields and boundaries) and writes, in the same private directory:

    rx-ops.json          every field's operations back to 2015: what was
                         planted each season, what came off and at what
                         yield, what was applied
    rx-yield/<op>.json   one per harvest pass: the yield monitor points
                         thinned to 20 m cells, as an index against the
                         field's own average

Why thinned: Deere's shapefile export of one harvest pass is section-level -
2.4 million points and a 50 MB zip for a 170-acre field - and there are
about 450 fields. The page needs where the field yields above and below
its own average, which a 20 m grid carries in about 2 KB. The zips are not
kept. A cell file that already exists is not fetched again, so a run that
is interrupted picks up where it stopped.

One operations call per field returns the whole history (it is the
cropSeason filter that makes jd_fleet's crop lookup cheap, not the API).
The shapefile link answers with a 307 to a signed S3 URL, and that second
request must NOT carry the bearer token - S3 refuses a request with two
credentials, and urllib forwards the header on redirect.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import io
import math
import pathlib
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, now_iso, read_json, write_private  # noqa: E402
from jd_fleet import ACCEPT, api, api_all, crop_word, refresh_token  # noqa: E402

FLEET = SECRETS / "fleet.json"
OPS = SECRETS / "rx-ops.json"
YIELD_DIR = SECRETS / "rx-yield"

CELL_M = 20.0                 # yield cell edge, metres
M_PER_DEG_LAT = 111_320.0
BU_PER_M3 = 28.377593         # US bushel = 35.239 L
AC_PER_HA = 2.4710538
SQFT_PER_AC = 43_560.0

_print_lock = threading.Lock()


def say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------- operations

def _rate(p: dict) -> dict:
    r = p.get("rate") or {}
    return {"name": p.get("name"), "type": p.get("productType"),
            "rate": r.get("value"), "unit": r.get("unitId")}


def _measure(m: dict | None, key: str) -> float | None:
    v = (m or {}).get(key) or {}
    return v.get("value")


def harvest_summary(token: str, op: dict) -> dict:
    """Deere's own totals for a harvest pass, in bushels and acres."""
    link = next((l["uri"] for l in op.get("links", [])
                 if l.get("rel") == "harvestYieldResult"), None)
    if not link:
        return {}
    status, body = api(token, link)
    if status != 200 or not isinstance(body, dict):
        return {}
    ha = _measure(body, "area")
    m3_ha = _measure(body, "averageYield")
    out = {}
    if ha is not None:
        out["acres"] = round(ha * AC_PER_HA, 2)
    if m3_ha is not None:
        out["avg"] = round(m3_ha * BU_PER_M3 / AC_PER_HA, 1)
    moist = _measure(body, "averageMoisture")
    if moist is not None:
        out["moisture"] = round(moist, 1)
    return out


def field_counties(token: str, orgs: list[str]) -> dict:
    """Field id -> the 'farm' it sits in, which this operation uses for the
    county. fleet.json does not carry it; the field list with farms embedded
    does, a hundred fields a page."""
    out = {}
    for oid in orgs:
        for f in api_all(token, f"/platform/organizations/{oid}/fields?embed=farms"):
            farms = ((f.get("farms") or {}).get("farms") or [])
            name = farms[0].get("name") if farms else None
            if f.get("id") and name and name != "---":
                out[f["id"]] = name
    return out


def field_ops(token: str, field: dict) -> dict:
    ops = api_all(token, f"/platform/organizations/{field['org']}/fields/"
                         f"{field['id']}/fieldOperations")
    out = []
    for o in ops:
        kind = str(o.get("fieldOperationType") or "").lower()
        rec = {"id": o.get("id"), "type": kind,
               "season": int(o.get("cropSeason") or 0) or None,
               "crop": crop_word(o.get("cropName") or o.get("treatedCropName")),
               "crop_code": o.get("cropName") or o.get("treatedCropName"),
               "start": o.get("startDate"), "end": o.get("endDate")}
        if kind == "application":
            rec["products"] = [_rate(p) for p in o.get("products", [])]
        if kind == "seeding":
            rec["varieties"] = [v.get("name") for v in o.get("varieties", [])
                                if v.get("name") and v["name"] != "---"]
        if kind == "harvest":
            rec["shapefile"] = next((l["uri"] for l in o.get("links", [])
                                     if l.get("rel") == "shapeFileAsync"), None)
            rec.update(harvest_summary(token, o))
        out.append(rec)
    out.sort(key=lambda r: (r["season"] or 0, r["start"] or ""))
    return {"name": field["name"], "org": field["org"], "id": field["id"],
            "fetched": now_iso(), "ops": out}


# ---------------------------------------------------------------- yield maps

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def download_shapefile(token: str, url: str) -> bytes | None:
    """The zip behind a shapeFileAsync link, or None if Deere will not give it.

    Deere answers 307 with a signed S3 URL once the export exists, and 202
    (or an empty 200) while it is still being built - poll a little.
    """
    for attempt in range(8):
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", ACCEPT)
        try:
            with _opener.open(req, timeout=120) as r:
                status, headers = r.status, r.headers
        except urllib.error.HTTPError as e:
            status, headers = e.code, e.headers
            if status in (301, 302, 303, 307, 308):
                pass
            elif status == 429 or status >= 500:
                time.sleep(10 * (attempt + 1))
                continue
            else:
                say(f"  shapefile {status} for {url}")
                return None
        except (urllib.error.URLError, OSError):
            # The connection dropped (DNS, reset, timeout). A farm internet
            # link does that; wait and try again rather than lose the run.
            time.sleep(20 * (attempt + 1))
            continue
        if status in (301, 302, 303, 307, 308):
            loc = headers.get("Location")
            try:
                with urllib.request.urlopen(loc, timeout=600) as s3:
                    return s3.read()
            except (urllib.error.URLError, OSError):
                time.sleep(20 * (attempt + 1))
                continue
        time.sleep(8 * (attempt + 1))
    return None


def _dbf_columns(dbf: bytes):
    import numpy as np
    nrec, hlen, rlen = struct.unpack("<IHH", dbf[4:12])
    fields, o = [], 32
    while dbf[o] != 0x0D:
        name = dbf[o:o + 11].split(b"\0")[0].decode("ascii", "replace")
        fields.append((name, dbf[o + 16]))
        o += 32
    dt = np.dtype([("_del", "S1")] + [(n, f"S{ln}") for n, ln in fields])
    if dt.itemsize != rlen:
        raise ValueError(f"dbf record {rlen} bytes, header says {dt.itemsize}")
    return np.frombuffer(dbf, dtype=dt, count=nrec, offset=hlen), nrec


def _shp_points(shp: bytes, nrec: int):
    import numpy as np
    shape_type = struct.unpack("<i", shp[32:36])[0]
    if shape_type != 1 or len(shp) - 100 != nrec * 28:
        raise ValueError(f"not a plain point shapefile (type {shape_type})")
    rec = np.frombuffer(shp, dtype=np.dtype([("h", "S8"), ("t", "<i4"),
                                              ("x", "<f8"), ("y", "<f8")]),
                        count=nrec, offset=100)
    return rec["x"], rec["y"]


def thin_yield(zip_bytes: bytes) -> dict | None:
    """Yield points -> 20 m cells of yield index (cell mean / field mean).

    Cleaning is the usual yield-monitor hygiene: drop zero and absurd
    readings (outside 0.15x-3x the median), and weight each point by the
    area it represents (distance x swath) so a slow crawl through a wet
    spot does not count more than the same ground at speed.
    """
    import numpy as np
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = z.namelist()
    dbf_name = next(n for n in names if n.lower().endswith(".dbf"))
    shp_name = next(n for n in names if n.lower().endswith(".shp"))
    rows, nrec = _dbf_columns(z.read(dbf_name))
    if nrec == 0:
        return None
    x, y = _shp_points(z.read(shp_name), nrec)

    def col(name):
        return np.char.strip(rows[name]).astype(float)

    yv = col("VRYIELDVOL")
    area = col("DISTANCE") * col("SWATHWIDTH")     # square feet
    moist = col("Moisture") if "Moisture" in rows.dtype.names else None
    keep = np.isfinite(yv) & (yv > 0) & (area > 0)
    if keep.sum() < 50:
        return None
    med = float(np.median(yv[keep]))
    keep &= (yv > 0.15 * med) & (yv < 3.0 * med)
    x, y, yv, area = x[keep], y[keep], yv[keep], area[keep]
    avg = float((yv * area).sum() / area.sum())

    lat0, lon0 = float(y.min()), float(x.min())
    lat_mid = float((y.min() + y.max()) / 2)
    dlat = CELL_M / M_PER_DEG_LAT
    dlon = CELL_M / (M_PER_DEG_LAT * math.cos(math.radians(lat_mid)))
    ix = np.floor((x - lon0) / dlon).astype(np.int64)
    iy = np.floor((y - lat0) / dlat).astype(np.int64)
    nx, ny = int(ix.max()) + 1, int(iy.max()) + 1
    cell = iy * nx + ix
    wsum = np.bincount(cell, weights=area, minlength=nx * ny)
    ysum = np.bincount(cell, weights=yv * area, minlength=nx * ny)
    cnt = np.bincount(cell, minlength=nx * ny)
    idx = np.zeros(nx * ny, dtype=np.uint8)
    ok = (cnt >= 3) & (wsum > 0)
    idx[ok] = np.clip(np.rint(ysum[ok] / wsum[ok] / avg * 100), 1, 255).astype(np.uint8)
    out = {"avg": round(avg, 1), "acres": round(float(area.sum()) / SQFT_PER_AC, 1),
           "n": int(nrec), "cell_m": CELL_M, "lon0": lon0, "lat0": lat0,
           "dlon": dlon, "dlat": dlat, "nx": nx, "ny": ny,
           "idx": base64.b64encode(idx.tobytes()).decode("ascii")}
    if moist is not None:
        m = moist[keep]
        out["moisture"] = round(float((m * area).sum() / area.sum()), 1)
    return out


def yield_job(token: str, rec: dict, op: dict) -> str:
    path = YIELD_DIR / f"{op['id']}.json"
    if path.exists():
        return "cached"
    t0 = time.time()
    data = download_shapefile(token, op["shapefile"])
    if data is None:
        return "no download"
    try:
        cells = thin_yield(data)
    except Exception as e:  # noqa: BLE001 - one bad export must not stop the run
        return f"parse failed: {type(e).__name__}: {e}"
    if cells is None:
        return "too few points"
    cells.update({"op": op["id"], "field": rec["name"], "field_id": rec["id"],
                  "org": rec["org"], "season": op["season"], "crop": op["crop"],
                  "fetched": now_iso()})
    write_private(path, cells, separators=(",", ":"))
    mb = len(data) / 1e6
    return f"{cells['avg']} bu/ac from {cells['n']:,} pts, {mb:.0f} MB in {time.time() - t0:.0f}s"


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seasons", nargs="*", type=int,
                    help="harvest seasons to pull yield maps for (default: 3 newest)")
    ap.add_argument("--no-yield", action="store_true", help="skip the yield maps")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch operation history even if cached")
    ap.add_argument("--field", action="append", help="only these field names")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    fleet = read_json(FLEET, None)
    if not fleet:
        sys.exit(f"no {FLEET} - run dev/jd_fleet.py first")
    fields = [f for f in fleet["fields"] if f.get("id")]
    if args.field:
        fields = [f for f in fields if f["name"] in set(args.field)]
    token = refresh_token()

    cache = read_json(OPS, {"fields": {}})
    by_id = cache.setdefault("fields", {})
    cache["county"] = field_counties(token, sorted({str(f["org"]) for f in fleet["fields"]}))
    say(f"counties for {len(cache['county'])} fields")
    todo = [f for f in fields if args.refresh or f["id"] not in by_id]
    say(f"{len(fields)} fields, {len(todo)} to fetch operations for")
    with cf.ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(field_ops, token, f): f for f in todo}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            f = futs[fut]
            try:
                by_id[f["id"]] = fut.result()
            except Exception as e:  # noqa: BLE001
                say(f"  {f['name']}: {type(e).__name__}: {e}")
            if i % 25 == 0 or i == len(todo):
                say(f"  operations {i}/{len(todo)}")
                cache["generated_at"] = now_iso()
                write_private(OPS, cache, separators=(",", ":"))
    cache["generated_at"] = now_iso()
    write_private(OPS, cache, separators=(",", ":"))
    n_ops = sum(len(r["ops"]) for r in by_id.values())
    say(f"wrote {OPS}: {len(by_id)} fields, {n_ops:,} operations")

    if args.no_yield:
        return
    YIELD_DIR.mkdir(exist_ok=True)
    wanted = {f["id"] for f in fields}
    harvests = [(rec, op) for rec in by_id.values() if rec["id"] in wanted
                for op in rec["ops"]
                if op["type"] == "harvest" and op.get("shapefile") and op.get("season")]
    seasons = args.seasons
    if not seasons:
        seasons = sorted({op["season"] for _r, op in harvests})[-3:]
    harvests = [(r, o) for r, o in harvests if o["season"] in seasons]
    pending = [(r, o) for r, o in harvests
               if not (YIELD_DIR / f"{o['id']}.json").exists()]
    say(f"yield maps: seasons {seasons}, {len(harvests)} harvest passes, "
        f"{len(pending)} still to fetch")
    with cf.ThreadPoolExecutor(min(args.workers, 2)) as pool:
        futs = {pool.submit(yield_job, token, r, o): (r, o) for r, o in pending}
        done = 0
        for fut in cf.as_completed(futs):
            r, o = futs[fut]
            done += 1
            try:
                msg = fut.result()
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
            say(f"  [{done}/{len(pending)}] {r['name']} {o['season']} {o['crop']}: {msg}")
            if done % 40 == 0:
                # Access tokens last 12 h and a full run can too. refresh_token
                # exits the process if Deere cannot be reached; a blip must not.
                try:
                    token = refresh_token()
                except SystemExit:
                    say("  token refresh failed (offline?) - carrying on with the old one")
    have = len(list(YIELD_DIR.glob("*.json")))
    say(f"{have} yield maps in {YIELD_DIR}")


if __name__ == "__main__":
    main()
