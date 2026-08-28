"""Filter the raw fertilizer-licensee pull down to likely NH3 dealers.

The state license list can't say who actually sells anhydrous (there's no NH3
license type), so this applies two passes:

1. Drop licensees that plainly aren't anhydrous retailers: grocery/hardware
   chains, egg and hog operations, lawn/turf/garden outfits, feed dealers,
   seed companies, manure applicators, wholesale-only suppliers, and bare
   office addresses.
2. Split the rest into CONFIRMED-STYLE ag retailers (co-op agronomy locations
   and national ag retail chains that customarily sell NH3) vs UNCERTAIN
   (custom applicators, liquid-fertilizer specialists, small independents).

Koch Fertilizer sites are wholesale NH3 terminals - farmers buy through the
retailers, so they're listed separately for reference, not as dealers.

Reads dev/nh3-licensees-raw.csv, writes dev/nh3-dealers-filtered.csv
"""
import csv
import re
from pathlib import Path

HERE = Path(__file__).parent

EXCLUDE = [
    # retail chains (bagged lawn fertilizer license)
    "dollar general", "fareway", "hy-vee", "wal-mart", "target stores",
    "menards", "fleet farm", "tractor supply", "bomgaars", "ace hardware",
    # lawn / turf / garden
    "lawn", "turf", "garden", "grass masters", "outdoor services",
    # egg / hog / livestock operations
    "pullet", "egg", "cage free", "daybreak foods", "rembrandt",
    "centrum valley", "hawkeye pride", "ovation", "opal foods",
    "christensen farms", "coulter",
    # feed / nutrition
    "feed service", "nutrition warehouse", "ag supply warehouse",
    # manure / spreading services
    "nutrient spreading", "r&d applications", "stateline ag llc",
    # seed / chem-only / wholesale
    "rockwell seed", "titan pro", "mosaic global", "van diest",
    # individuals and single farms
    "tim lodin", "chris jensen", "thilges", "harvey farms", "plagge farms",
    "banwart", "lizard", "enrich", "ket enterprises", "dj wempen",
    "tj agrifactory", "hampton farm", "return", "service ltd",
]

# wholesale NH3 terminals - real anhydrous, but not retail
TERMINAL = ["koch fertilizer"]

# companies whose agronomy locations customarily retail NH3 in this area
CONFIRMED = [
    "gold-eagle", "gold eagle", "new coop", "new cooperative", "new horizon",
    "landus", "stateline cooperative", "five star", "nexus", "north iowa coop",
    "agvantage", "nutrien", "pro cooperative", "farmers coop", "farmers cooperative",
]

def classify(name: str) -> str:
    n = name.lower()
    for pat in TERMINAL:
        if pat in n:
            return "terminal"
    for pat in EXCLUDE:
        if pat in n:
            return "exclude"
    for pat in CONFIRMED:
        if pat in n:
            return "dealer"
    return "uncertain"


def norm_addr(street: str, city: str) -> str:
    s = street.lower()
    s = re.sub(r"\b(po box|p\.o\. box|box)\s*\d+\b", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s + "|" + re.sub(r"[^a-z]", "", city.lower())


def clean_street(street: str) -> str:
    """Strip site-name prefixes like 'HUTCHINS - ' and PO boxes for geocoding."""
    s = re.sub(r"^[A-Za-z ]+ - ", "", street)          # 'HOLMES - 2150 ...'
    s = re.sub(r"-?\s*(PO BOX|P\.O\. BOX|BOX)\s*\d+", "", s, flags=re.I)
    return s.strip(" -")


def main() -> None:
    rows = list(csv.DictReader((HERE / "nh3-licensees-raw.csv").open(encoding="utf-8")))
    seen: dict[str, dict] = {}
    for r in rows:
        r["class"] = classify(r["facility"] + " " + r["company"])
        if r["class"] == "exclude":
            continue
        # Landus HQ office slipped into a county listing - not a plant
        if "des moines" in r["city"].lower():
            continue
        key = norm_addr(r["street"], r["city"])
        keep = seen.get(key)
        if keep is None or (keep["class"] != "dealer" and r["class"] == "dealer"):
            r["geocode_street"] = clean_street(r["street"])
            seen[key] = r

    out_rows = sorted(seen.values(), key=lambda r: (r["class"], r["county"], r["city"], r["facility"]))
    out = HERE / "nh3-dealers-filtered.csv"
    fields = ["class", "facility", "company", "street", "geocode_street", "city", "zip", "county", "license", "expires"]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)

    counts: dict[str, int] = {}
    for r in out_rows:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    print(f"wrote {len(out_rows)} rows to {out}  {counts}")
    for r in out_rows:
        if r["class"] != "dealer":
            print(f"  [{r['class']}] {r['facility']} - {r['city']}")


if __name__ == "__main__":
    main()
