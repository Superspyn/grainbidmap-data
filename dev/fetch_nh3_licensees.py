"""Pull commercial fertilizer licensees from the Iowa Dept of Ag license search.

The search at https://iowadeptag.my.site.com/s/searchlicense is a public
Salesforce site; its Apex controller answers guest requests, so this asks it
the same question the page's own Search button does, once per county.

There is no NH3-specific license type in the portal (only Ag Lime / Egg /
Feed / Fertilizer), so this fetches every Fertilizer License holder and a
separate pass filters out retail stores, egg/hog operations, lawn-care
outfits, and other licensees that plainly aren't anhydrous dealers.

Usage:
    python dev/fetch_nh3_licensees.py            # the 8 counties around Britt
    python dev/fetch_nh3_licensees.py all        # every Iowa county (slow-ish)

Writes dev/nh3-licensees-raw.csv (everything) - filtering happens elsewhere.
"""
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

AURA_URL = "https://iowadeptag.my.site.com/s/sfsites/aura?r=1&aura.ApexAction.execute=1"

# fwuid changes when Salesforce upgrades the site; if requests start failing,
# load the search page in a browser and copy the current value from any
# /s/sfsites/aura request body.
AURA_CONTEXT = {
    "mode": "PROD",
    "fwuid": "MzNzN1lSdDZQRXpUcEpsWHBlZGd5UWtVMjdnTGFERUU2S3FfSVdrcU92bkExNC4xOTIuODM4ODYwOA",
    "app": "siteforce:communityApp",
    "loaded": {"APPLICATION@markup://siteforce:communityApp": "1711_ugTLRbFsaJuuXl52zUPOqA"},
    "dn": [],
    "globals": {},
    "uad": True,
}

# Hancock county (Britt) and its neighbors.
HOME_COUNTIES = [
    "HANCOCK - 41", "WINNEBAGO - 95", "WORTH - 98", "CERRO GORDO - 17",
    "FRANKLIN - 35", "WRIGHT - 99", "HUMBOLDT - 46", "KOSSUTH - 55",
]


def fetch_county(county: str) -> list[dict]:
    message = {
        "actions": [{
            "id": "1;a",
            "descriptor": "aura://ApexActionController/ACTION$execute",
            "callingDescriptor": "UNKNOWN",
            "params": {
                "namespace": "",
                "classname": "lp_SearchLicenseController",
                "method": "getLicenses",
                "params": {"labelType": "Fertilizer License", "countyName": county},
                "cacheable": False,
                "isContinuation": False,
            },
        }]
    }
    body = urllib.parse.urlencode({
        "message": json.dumps(message),
        "aura.context": json.dumps(AURA_CONTEXT),
        "aura.pageURI": "/s/searchlicense",
        "aura.token": "null",
    }).encode()
    req = urllib.request.Request(AURA_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    action = payload["actions"][0]
    if action["state"] != "SUCCESS":
        raise RuntimeError(f"{county}: {json.dumps(action)[:300]}")
    return action["returnValue"]["returnValue"] or []


def all_counties() -> list[str]:
    """The portal's own county picklist, fetched the same way."""
    message = {
        "actions": [{
            "id": "1;a",
            "descriptor": "aura://ApexActionController/ACTION$execute",
            "callingDescriptor": "UNKNOWN",
            "params": {
                "namespace": "",
                "classname": "lp_SearchLicenseController",
                "method": "getCountyList",
                "params": {},
                "cacheable": True,
                "isContinuation": False,
            },
        }]
    }
    body = urllib.parse.urlencode({
        "message": json.dumps(message),
        "aura.context": json.dumps(AURA_CONTEXT),
        "aura.pageURI": "/s/searchlicense",
        "aura.token": "null",
    }).encode()
    req = urllib.request.Request(AURA_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    return payload["actions"][0]["returnValue"]["returnValue"]


def main() -> None:
    counties = HOME_COUNTIES
    if len(sys.argv) > 1 and sys.argv[1] == "all":
        counties = all_counties()

    rows = []
    for county in counties:
        entries = fetch_county(county)
        print(f"{county}: {len(entries)}")
        for e in entries:
            loc = (e.get("currentLocation") or {}).get("location") or {}
            rows.append({
                "facility": e.get("facilityName", ""),
                "company": e.get("companyName", ""),
                "street": e.get("streetAddress", ""),
                "city": e.get("city", ""),
                "zip": (loc.get("PostalCode") or "").split("-")[0],
                "county": e.get("county", "").split(" - ")[0],
                "license": e.get("licenseNumber", ""),
                "expires": e.get("licenseExirationDate", ""),
            })
        time.sleep(1)  # be polite - the UI would make one request per click too

    out = Path(__file__).parent / "nh3-licensees-raw.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
