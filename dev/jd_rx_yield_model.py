"""Fit the farm's own corn yield model, for the planner on the page.

    python dev/jd_rx_yield_model.py          fit and print
    (dev/jd_build_rx.py imports fit_model() and bakes the result)

Every corn harvest in Operations Center is one row: the field's average
yield that year against the ground and the season.

What the record can and cannot support, tested by leaving each year out
and predicting it from the others:

  * The soil survey rating under the field predicts yield across fields.
    Rating alone predicts a held-out year to within 32.7 bu, against
    37.3 for guessing the farm mean. Every weather term added on top
    made the held-out error WORSE, because eleven seasons all sharing
    the same weather is eleven data points however many fields there
    are, and the fit learns the seasons' quirks, not the weather.

So the model is deliberately small:

    yield = a + b x rating + (season effect)

with the season effect being each year's farm-wide departure from what
the ratings predicted - 2019 ran high, 2020 (drought, then the derecho)
ran 30 bu low. The planner picks a season by year, or by weather (the
years on record most like the GDD and grain-fill rain asked for), so
the weather inputs act through seasons the farm actually lived through
rather than through a coefficient the data cannot pin down.

Also reported, from a fuller fit, because they are worth knowing even
though they do not improve prediction: bushels per day of hybrid
maturity and per day of planting delay, on this farm's records.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, read_json  # noqa: E402
from jd_build_rx import condense_ops, crop_family  # noqa: E402

OPS = SECRETS / "rx-ops.json"
SURVEY = SECRETS / "ssurgo.json"
WEATHER = SECRETS / "weather.json"
MIN_ACRES = 40

# Pioneer corn: P + four digits, the first two being the relative maturity
# less 100 (or the maturity itself in the 90s). P0421Q -> 104, P1185Q ->
# 111, P9929AMXT -> 99. Soybeans read P28Z30E and never match this.
PIONEER = re.compile(r"^P(\d{2})(\d{2})[A-Z]", re.I)


def hybrid_rm(name: str | None) -> int | None:
    if not name:
        return None
    m = PIONEER.match(name.strip())
    if not m:
        return None
    a = int(m.group(1))
    return 100 + a if a < 50 else a


def rating_of(rec: dict, units: dict) -> float | None:
    num = den = 0.0
    for pc in rec.get("pieces", []):
        u = units.get(pc["mukey"])
        if not u:
            continue
        c, n = u.get("csr2"), (u.get("nccpi") or {}).get("all")
        v = (c + n) / 2 if (c is not None and n is not None) else (c if c is not None else n)
        if v is None:
            continue
        num += v * pc["ac"]
        den += pc["ac"]
    return num / den if den else None


def rows(crop: str = "corn") -> list[dict]:
    ops = read_json(OPS, {}).get("fields", {})
    survey = read_json(SURVEY, {})
    weather = read_json(WEATHER, {})
    units = survey.get("mapunits", {})
    ratings = {n: rating_of(r, units) for n, r in survey.get("fields", {}).items()}
    cells, fcell = weather.get("cells", {}), weather.get("fields", {})
    out = []
    for rec in ops.values():
        name = rec["name"]
        seasons = condense_ops(rec)["seasons"]
        for yr, s in seasons.items():
            h = s.get("harvest") or {}
            if crop_family(h.get("crop")) != crop or h.get("avg") is None:
                continue
            if (h.get("acres") or 0) < MIN_ACRES:
                continue
            w = (cells.get(fcell.get(name, ""), {}).get("years") or {}).get(str(yr))
            r = ratings.get(name)
            if not w or r is None:
                continue
            rms = [x for x in (hybrid_rm(v) for v in (s.get("varieties") or [])) if x]
            planted = s.get("planted")
            doy = dt.date.fromisoformat(planted).timetuple().tm_yday if planted else None
            out.append({"field": name, "year": int(yr), "yield": h["avg"], "acres": h["acres"],
                        "rating": r, "gdd": w["gdd"], "gdd_frost": w["gdd_frost"], "rain": w["rain"],
                        "rain_fill": w["rain_fill"], "heat_days": w["heat_days"], "wet_days": w["wet_days"],
                        "rm": sum(rms) / len(rms) if rms else None, "plant_doy": doy})
    return out


def _ols(data: list[dict], feats: list[str], fill: dict | None = None) -> np.ndarray:
    fill = fill or {}
    X = np.array([[1.0] + [float(d[k] if d[k] is not None else fill[k]) for k in feats] for d in data])
    y = np.array([d["yield"] for d in data])
    w = np.sqrt(np.array([d["acres"] for d in data]))   # bigger fields count more
    beta, *_ = np.linalg.lstsq(X * w[:, None], y * w, rcond=None)
    return beta


def fit_model(crop: str = "corn") -> dict | None:
    data = rows(crop)
    if len(data) < 30:
        return None
    years = sorted({d["year"] for d in data})

    # 1. The ground: yield against rating, with a year effect for each season
    #    (fit jointly, so a run of good years does not get charged to rating).
    yr_idx = {y: i for i, y in enumerate(years)}
    X = np.zeros((len(data), 2 + len(years)))
    for i, d in enumerate(data):
        X[i, 0] = 1.0
        X[i, 1] = d["rating"]
        X[i, 2 + yr_idx[d["year"]]] = 1.0
    y = np.array([d["yield"] for d in data])
    w = np.sqrt(np.array([d["acres"] for d in data]))
    # the year dummies are constrained to average zero by dropping one and
    # re-centring afterwards, which keeps "a" meaningful as a normal year
    Xr = X[:, :-1]
    beta, *_ = np.linalg.lstsq(Xr * w[:, None], y * w, rcond=None)
    a, b = float(beta[0]), float(beta[1])
    eff = list(beta[2:]) + [0.0]
    mean_eff = sum(eff) / len(eff)
    season = {str(yr): float(e - mean_eff) for yr, e in zip(years, eff)}
    a += mean_eff
    pred = a + b * X[:, 1] + np.array([season[str(d["year"])] for d in data])
    resid = y - pred
    rmse = float(np.sqrt(np.mean(resid ** 2)))
    r2 = 1 - float(np.sum(resid ** 2)) / float(np.sum((y - y.mean()) ** 2))
    # the same without the season term: what a field-level guess is worth
    b0 = _ols(data, ["rating"])
    resid0 = y - (b0[0] + b0[1] * X[:, 1])
    rmse_no_season = float(np.sqrt(np.mean(resid0 ** 2)))

    # 2. Each season's weather, farm-wide, for the planner's analog search.
    wx = {}
    for yr in years:
        rs = [d for d in data if d["year"] == yr]
        wx[str(yr)] = {k: float(np.mean([d[k] for d in rs]))
                       for k in ("gdd", "gdd_frost", "rain", "rain_fill", "heat_days", "wet_days")}
        wx[str(yr)]["n"] = len(rs)
        wx[str(yr)]["yield"] = float(np.mean([d["yield"] for d in rs]))

    # 3. Hybrid maturity and planting date, from a fuller fit with the year
    #    effects in it, reported for what they are.
    med = {k: float(np.median([d[k] for d in data if d[k] is not None])) for k in ("rm", "plant_doy")}
    full = _ols([dict(d, **{f"y{yy}": 1.0 if d["year"] == yy else 0.0 for yy in years[:-1]}) for d in data],
                ["rating", "rm", "plant_doy"] + [f"y{yy}" for yy in years[:-1]], med)
    have_rm = sum(1 for d in data if d["rm"] is not None)
    have_doy = sum(1 for d in data if d["plant_doy"] is not None)

    return {"crop": crop, "n": len(data), "fields": len({d["field"] for d in data}), "years": years,
            "a": a, "b": b, "season": season, "rmse": rmse, "r2": r2, "rmse_no_season": rmse_no_season,
            "yield_mean": float(y.mean()), "yield_sd": float(y.std()),
            "rating_mean": float(X[:, 1].mean()), "wx": wx,
            "rm_coef": float(full[2]), "doy_coef": float(full[3]), "rm_n": have_rm, "doy_n": have_doy,
            "rm_median": med["rm"], "doy_median": med["plant_doy"]}


def main() -> None:
    m = fit_model("corn")
    if not m:
        sys.exit("not enough rows - is weather.json there?")
    print(f"corn: {m['n']} field-years over {m['fields']} fields, {m['years'][0]}-{m['years'][-1]}")
    print(f"  yield = {m['a']:.1f} + {m['b']:.2f} x rating + season   R2 {m['r2']:.2f}  RMSE {m['rmse']:.1f} bu "
          f"(rating alone {m['rmse_no_season']:.1f}; farm mean {m['yield_mean']:.1f}, sd {m['yield_sd']:.1f})")
    print("  season effects (bu):")
    for yr in m["years"]:
        w = m["wx"][str(yr)]
        print(f"    {yr} {m['season'][str(yr)]:+6.1f}   n {w['n']:3d}  GDD {w['gdd']:5.0f}  fill rain {w['rain_fill']:4.1f} in  "
              f"heat {w['heat_days']:3.1f}  wet {w['wet_days']:3.1f}")
    print(f"  hybrid maturity {m['rm_coef']:+.2f} bu per RM day (on {m['rm_n']} rows with a Pioneer name); "
          f"planting {m['doy_coef']:+.2f} bu per day later (on {m['doy_n']} rows)")


if __name__ == "__main__":
    main()
