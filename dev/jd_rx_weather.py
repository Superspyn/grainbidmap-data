"""Growing-season weather for every field, from NASA POWER.

    python dev/jd_rx_weather.py            fetch what is missing
    python dev/jd_rx_weather.py --refresh  fetch everything again

Reads ~/.grain-map-secrets/fleet.json and writes
~/.grain-map-secrets/weather.json, which dev/jd_build_rx.py bakes into the
page and fits the yield model on. NASA POWER is a public daily weather
service on a half-degree grid, no sign-in, no key; the farm's fields fall
in about two dozen grid cells, so that is two dozen requests.

For each cell and each year, from the daily maximum and minimum
temperature and precipitation:

  gdd        growing degree days May 1 - Sep 30, base 50F, cap 86F, the
             usual corn accounting
  gdd_frost  growing degree days from May 1 to the first fall frost
             (first day after Sep 1 with a minimum at or under 32F),
             which is what a hybrid actually has to finish in
  frost      the date of that first fall frost
  rain       precipitation Apr 1 - Sep 30, inches
  rain_fill  precipitation Jul 1 - Aug 31, inches: grain fill, where a
             dry spell costs the most
  heat_days  days Jul 1 - Aug 15 with a maximum at or over 95F, which is
             when heat during pollination and early fill takes yield
  wet_days   days Apr 15 - May 31 with over half an inch, the planting
             window

and the average of each over the record, as the location's normal.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, now_iso, read_json, write_private  # noqa: E402

FLEET = SECRETS / "fleet.json"
OUTPUT = SECRETS / "weather.json"
POWER = "https://power.larc.nasa.gov/api/temporal/daily/point"
FIRST_YEAR = 2005
LAST_YEAR = 2025
# POWER's grid: half a degree of latitude by five eighths of longitude.
LAT_STEP, LON_STEP = 0.5, 0.625


def cell_of(lat: float, lon: float) -> tuple[float, float]:
    return round(lat / LAT_STEP) * LAT_STEP, round(lon / LON_STEP) * LON_STEP


def fetch_daily(lat: float, lon: float, y0: int, y1: int) -> dict:
    url = (f"{POWER}?parameters=T2M_MAX,T2M_MIN,PRECTOTCORR&community=AG"
           f"&longitude={lon}&latitude={lat}&start={y0}0101&end={y1}1231&format=JSON")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=240) as r:
                return json.load(r)["properties"]["parameter"]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if attempt == 3:
                raise RuntimeError(f"POWER failed for {lat},{lon}: {e}") from e
            time.sleep(5 * (attempt + 1))
    return {}


def f_of(c: float) -> float:
    return c * 9 / 5 + 32


def season_stats(daily: dict, year: int) -> dict | None:
    tmax, tmin, rain = daily["T2M_MAX"], daily["T2M_MIN"], daily["PRECTOTCORR"]
    gdd = gdd_frost = 0.0
    rain_in = rain_fill = 0.0
    heat = wet = 0
    frost = None
    have = 0
    d = dt.date(year, 1, 1)
    while d.year == year:
        k = d.strftime("%Y%m%d")
        hi, lo, pr = tmax.get(k), tmin.get(k), rain.get(k)
        if hi is None or lo is None or hi < -900 or lo < -900:
            d += dt.timedelta(days=1)
            continue
        have += 1
        hi_f, lo_f = f_of(hi), f_of(lo)
        day = (min(hi_f, 86) + max(lo_f, 50)) / 2 - 50
        day = max(0.0, day)
        m, dd = d.month, d.day
        if dt.date(year, 5, 1) <= d <= dt.date(year, 9, 30):
            gdd += day
        if frost is None and d >= dt.date(year, 9, 1) and lo_f <= 32:
            frost = d.isoformat()
        if d >= dt.date(year, 5, 1) and frost is None:
            gdd_frost += day
        if pr is not None and pr >= 0:
            inch = pr / 25.4
            if 4 <= m <= 9:
                rain_in += inch
            if m in (7, 8):
                rain_fill += inch
            if dt.date(year, 4, 15) <= d <= dt.date(year, 5, 31) and inch > 0.5:
                wet += 1
        if dt.date(year, 7, 1) <= d <= dt.date(year, 8, 15) and hi_f >= 95:
            heat += 1
        d += dt.timedelta(days=1)
    if have < 300:
        return None
    return {"gdd": round(gdd), "gdd_frost": round(gdd_frost), "frost": frost,
            "rain": round(rain_in, 1), "rain_fill": round(rain_fill, 1),
            "heat_days": heat, "wet_days": wet}


def normals(years: dict) -> dict:
    keys = ("gdd", "gdd_frost", "rain", "rain_fill", "heat_days", "wet_days")
    out = {}
    vals = [v for v in years.values() if v]
    for k in keys:
        xs = [v[k] for v in vals if v.get(k) is not None]
        if xs:
            out[k] = round(sum(xs) / len(xs), 1)
            out[k + "_sd"] = round((sum((x - out[k]) ** 2 for x in xs) / max(1, len(xs) - 1)) ** 0.5, 1)
    frosts = [dt.date.fromisoformat(v["frost"]).timetuple().tm_yday for v in vals if v.get("frost")]
    if frosts:
        doy = round(sum(frosts) / len(frosts))
        out["frost_doy"] = doy
        out["frost"] = (dt.date(2001, 1, 1) + dt.timedelta(days=doy - 1)).strftime("%b %d")
    return out


def main() -> None:
    refresh = "--refresh" in sys.argv
    fleet = read_json(FLEET, None)
    if not fleet:
        sys.exit(f"no {FLEET} - run dev/jd_fleet.py first")
    store = {} if refresh else read_json(OUTPUT, {})
    cells: dict = store.get("cells", {})
    field_cell: dict = {}
    for f in fleet["fields"]:
        if not f.get("rings"):
            continue
        lat, lon = cell_of(float(f["lat"]), float(f["lon"]))
        field_cell[f["name"]] = f"{lat},{lon}"
    todo = sorted(set(field_cell.values()) - set(cells))
    print(f"{len(field_cell)} fields in {len(set(field_cell.values()))} cells, {len(todo)} to fetch")
    for i, key in enumerate(todo, 1):
        lat, lon = (float(x) for x in key.split(","))
        daily = fetch_daily(lat, lon, FIRST_YEAR, LAST_YEAR)
        years = {}
        for y in range(FIRST_YEAR, LAST_YEAR + 1):
            st = season_stats(daily, y)
            if st:
                years[str(y)] = st
        cells[key] = {"lat": lat, "lon": lon, "years": years, "normal": normals(years)}
        write_private(OUTPUT, {"generated_at": now_iso(), "source": "NASA POWER daily, AG community",
                               "cells": cells, "fields": field_cell})
        n = cells[key]["normal"]
        print(f"  {i}/{len(todo)} {key}: {len(years)} years, normal GDD {n.get('gdd')} "
              f"rain {n.get('rain')} in, frost {n.get('frost')}")
    write_private(OUTPUT, {"generated_at": now_iso(), "source": "NASA POWER daily, AG community",
                           "cells": cells, "fields": field_cell})
    print(f"wrote {OUTPUT}: {len(cells)} cells, {len(field_cell)} fields")


if __name__ == "__main__":
    main()
