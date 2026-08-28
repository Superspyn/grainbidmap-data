"""Turn dev/nh3-dealers-geocoded.csv into the nfDealers array in nh3-map.html."""
import csv
import re
from pathlib import Path

HERE = Path(__file__).parent
MAP = HERE.parent / "nh3-map.html"

COMPANY = [
    ("gold-eagle", "Gold-Eagle Coop"), ("gold eagle", "Gold-Eagle Coop"),
    ("new horizon", "New Horizon Coop"),
    ("new coop", "NEW Cooperative"), ("new cooperative", "NEW Cooperative"),
    ("landus", "Landus"),
    ("stateline cooperative", "Stateline Coop"),
    ("five star", "Five Star Coop"),
    ("nexus", "Nexus Cooperative"),
    ("north iowa coop", "North Iowa Coop"),
    ("agvantage", "AgVantage FS"),
    ("nutrien", "Nutrien Ag Solutions"),
    ("pro cooperative", "Pro Cooperative"),
    ("farmers coop", "Farmers Coop"), ("farmers cooperative", "Farmers Coop"),
    ("helena", "Helena"),
    ("liqui-grow", "Liqui-Grow"),
    ("koch fertilizer", "Koch Fertilizer (terminal)"),
    ("asmus", "Asmus Farm Supply"),
]


def company_for(facility: str) -> str:
    n = facility.lower()
    for pat, clean in COMPANY:
        if pat in n:
            return clean
    return facility.title()


def town_case(city: str) -> str:
    return " ".join(w.capitalize() for w in city.split())


def main() -> None:
    rows = list(csv.DictReader((HERE / "nh3-dealers-geocoded.csv").open(encoding="utf-8")))
    rows = [r for r in rows if r["lat"]]

    # name pins "Company Town"; disambiguate same-town duplicates by street
    counts: dict[str, int] = {}
    for r in rows:
        r["_company"] = company_for(r["facility"])
        r["_town"] = town_case(r["city"])
        r["_base"] = f'{r["_company"]} {r["_town"]}'
        counts[r["_base"]] = counts.get(r["_base"], 0) + 1

    lines = []
    for r in sorted(rows, key=lambda r: (r["_company"], r["_town"], r["street"])):
        name = r["_base"]
        if counts[r["_base"]] > 1:
            street = r["geocode_street"] or r["street"]
            name += f' ({street.title()})'
        name = name.replace("'", "\\'")
        parts = [
            f"name: '{name}'",
            f"company: '{r['_company']}'",
            f"town: '{r['_town']}'",
            f"lat: {float(r['lat']):.6f}",
            f"lng: {float(r['lng']):.6f}",
        ]
        if r["class"] == "uncertain":
            parts.append("unsure: 1")
        if r["class"] == "terminal":
            parts.append("terminal: 1")
        if r["approx"] == "1":
            parts.append("approx: 1")
        lines.append("    { " + ", ".join(parts) + " },")
    array_js = "\n".join(lines).rstrip(",")

    html = MAP.read_text(encoding="utf-8")
    new_html, n = re.subn(
        r"(var nfDealers = \[\n).*?(\n  \];)",
        lambda m: m.group(1) + array_js + m.group(2),
        html,
        flags=re.S,
    )
    if n != 1:
        raise SystemExit("could not find nfDealers array in nh3-map.html")
    MAP.write_text(new_html, encoding="utf-8")
    print(f"wrote {len(lines)} pins into {MAP.name}")


if __name__ == "__main__":
    main()
