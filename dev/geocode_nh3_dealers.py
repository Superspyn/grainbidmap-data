"""Geocode the filtered NH3 dealer list.

Three passes, best first:
1. US Census batch geocoder (free, no key, good with real street addresses)
2. Nominatim per-address (rural 'HWY 69' style addresses the Census misses)
3. Nominatim city centroid, flagged approx=1 so the map note can say
   "pin is on the town, not the driveway" and it can be hand-fixed later.

Reads dev/nh3-dealers-filtered.csv, writes dev/nh3-dealers-geocoded.csv
"""
import csv
import io
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
UA = {"User-Agent": "grain-map nh3 dealer geocoder (accesselistuder@gmail.com)"}


def census_batch(rows: list[dict]) -> dict[str, tuple[float, float]]:
    buf = io.StringIO()
    w = csv.writer(buf)
    for i, r in enumerate(rows):
        w.writerow([i, r["geocode_street"], r["city"], "IA", r["zip"]])
    boundary = "----nh3geocode"
    payload = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="benchmark"\r\n\r\n'
        "Public_AR_Current\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="addressFile"; filename="addr.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
        f"{buf.getvalue()}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        "https://geocoding.geo.census.gov/geocoder/locations/addressbatch",
        data=payload,
        headers={**UA, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = resp.read().decode()
    out: dict[str, tuple[float, float]] = {}
    for line in csv.reader(io.StringIO(text)):
        # id, input addr, match?, exact?, matched addr, "lng,lat", tigerline, side
        if len(line) >= 6 and line[2] == "Match" and line[5]:
            lng, lat = line[5].split(",")
            out[line[0]] = (float(lat), float(lng))
    return out


def nominatim(query: str) -> tuple[float, float] | None:
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1, "countrycodes": "us"}
    )
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            hits = json.loads(resp.read().decode())
    except Exception:
        return None
    if hits:
        return float(hits[0]["lat"]), float(hits[0]["lon"])
    return None


def main() -> None:
    rows = list(csv.DictReader((HERE / "nh3-dealers-filtered.csv").open(encoding="utf-8")))
    print(f"{len(rows)} rows; census batch pass...")
    matched = census_batch(rows)
    print(f"census matched {len(matched)}")

    city_cache: dict[str, tuple[float, float] | None] = {}
    for i, r in enumerate(rows):
        r["approx"] = "0"
        hit = matched.get(str(i))
        if hit is None and r["geocode_street"]:
            hit = nominatim(f'{r["geocode_street"]}, {r["city"]}, IA {r["zip"]}')
            time.sleep(1.1)
            if hit:
                print(f"  nominatim: {r['facility']} ({r['city']})")
        if hit is None:
            key = (r["city"] + "|" + r["zip"]).lower()
            if key not in city_cache:
                city_cache[key] = nominatim(f'{r["city"]}, IA {r["zip"]}') or nominatim(f'{r["city"]}, Iowa')
                time.sleep(1.1)
            hit = city_cache[key]
            r["approx"] = "1"
            print(f"  city-centroid: {r['facility']} ({r['city']})" + ("" if hit else "  ** NO MATCH AT ALL"))
        r["lat"], r["lng"] = (f"{hit[0]:.6f}", f"{hit[1]:.6f}") if hit else ("", "")

    out = HERE / "nh3-dealers-geocoded.csv"
    fields = list(rows[0].keys())
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    n_approx = sum(1 for r in rows if r["approx"] == "1")
    print(f"wrote {len(rows)} rows to {out} ({n_approx} at city centroid)")


if __name__ == "__main__":
    main()
