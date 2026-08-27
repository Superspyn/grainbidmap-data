# Grain Hauling Cost Map — cash bid scraper

Scrapes corn and soybean cash bids for the elevators pinned on the Grain Hauling
Cost Map, publishes them as a single JSON file, and lets the map show each
elevator's full bid table (delivery period, futures month, futures, basis, cash)
instead of making you look bids up and type them in by hand.

Clicking a row in that table drops the bid into the existing calculator, so the
"net price after all costs" number updates for that specific delivery period.

---

## Setup (one time)

### 1. Push this repo to GitHub

A **public** repo is simplest — GitHub Pages on a private repo needs a paid
plan, and cash bids are public information either way.

```bash
git remote add origin https://github.com/<you>/grain-map.git
git push -u origin main
```

### 2. Turn on GitHub Pages

Repo **Settings → Pages → Source: Deploy from a branch**, branch `main`,
folder `/docs`. Your feed will then be at:

```
https://<you>.github.io/grain-map/bids.json
```

Pages serves `Access-Control-Allow-Origin: *`, which is what lets the map fetch
it from Squarespace.

### 3. Point the map at that URL

In `grain-trucking-map.html`, edit the placeholder near the top:

```js
var GT_BIDS_URL = 'https://REPLACE-ME.github.io/grain-map/bids.json';
```

Until that's set, the map simply skips the feed and behaves exactly as it did
before — manual bid entry, "View bid page" links.

### 4. Paste the updated HTML into Squarespace

Copy all of `grain-trucking-map.html` into the code block. **Re-export it back
into this repo whenever you edit it on the site**, or the two will drift.

### 5. Let the workflow run

`.github/workflows/bids.yml` runs twice each weekday after the 1:15 PM CT close.
Run it by hand the first time from the **Actions** tab → *Update cash bids* →
*Run workflow*.

---

## What's covered

Bids come from platform endpoints that return every location for a co-op in a
single request, so the whole scrape costs about six HTTP requests.

| Source | Pins | How |
|---|---|---|
| Heartland Co-op | 53 | Server-rendered bid sheet |
| CVA | 36 | AgriCharts |
| Gold-Eagle Co-op | 24 | AgriCharts |
| Pro Co-op | 14 | AgriCharts |
| North Iowa Co-op | 4 | AgriCharts |
| Mid-Iowa Milling | 2 | AgriCharts |

**133 of 722 pins** currently carry live bids. Two things account for the rest:

- **554 pins are on companies with no adapter yet.** The biggest are NEW Co-op
  (71), MFA (59), Landus (51) and Cargill (31). See *Adding a source* below.
- **33 pins matched a source that publishes no bid for them** — Heartland's
  Texas, Kansas and Nebraska sites and its seasonal locations, plus CVA's Kansas
  locations. There is no bid to show; those pins keep the "View bid page" link.

Uncovered pins are not broken — they behave exactly as they did before.

---

## Adding a source

Most co-ops run an AgriCharts widget, which needs no code at all — just the
tenant subdomain:

1. Open the co-op's cash-bids page and view source.
2. Search for `agricharts.com`. The widget's `src` looks like
   `//<tenant>.agricharts.com/inc/cashbids/cashbids.php?...`.
3. Add it to `scrapers/config/sources.yaml`, and map the map's `company` name to
   it in `COMPANY_TO_SOURCE` in `scrapers/match_locations.py`.
4. Re-run the matching step below.

A wrong subdomain returns a 1,880-byte "Site Not Configured" page rather than an
error, so check the location count.

Anything not on AgriCharts needs an adapter in `scrapers/adapters/` implementing
the `Adapter` protocol in `adapters/base.py`.

---

## Re-running the pin matching

Only needed after adding a source, or after adding/renaming pins on the map.
It is a **hand-reviewed, one-time** step, not part of the scheduled run.

```bash
python scrapers/build_bids.py --raw
python scrapers/match_locations.py --report
```

That writes `scrapers/config/location_map.json`. **Read the `needs_review` and
`unmatched` sections before committing it.** Matches below the confidence
threshold are deliberately *excluded* from the published mapping — showing the
wrong elevator's bid is worse than showing none — so promote one by hand if you
have checked it.

Pin IDs are slugs of the pin name (`Gold-Eagle Coop Clarion` →
`gold-eagle-coop-clarion`), de-duplicated in list order. The map derives the same
slug at runtime, so `gtLocations` needs no extra field.

---

## Development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r scrapers/requirements.txt
```

| Command | What it does |
|---|---|
| `python scrapers/build_bids.py` | Scrape everything, write `docs/bids.json` |
| `python scrapers/build_bids.py --source heartland --dry-run` | Print one source as a table, write nothing |
| `python scrapers/build_bids.py --cache` | Reuse cached HTTP responses (don't re-hit live sites) |
| `python scrapers/build_bids.py --validate docs/bids.json` | Schema and staleness check |
| `python -m pytest scrapers/tests -q` | Offline tests against saved fixtures |

### Testing the map locally

The Google Maps key is referrer-restricted, so the real map won't load on
localhost. `dev/bids-test.html` stubs the Maps API so the bid integration can be
exercised end to end anyway:

```bash
python -m http.server 8765
```

Then open `http://127.0.0.1:8765/dev/bids-test.html` for the live feed, or
`?feed=404` to confirm the degraded path leaves the tool unchanged.

---

## Scraping etiquette

These are public bid pages belonging to small co-ops, and `scrapers/fetch.py` is
deliberately conservative: a descriptive User-Agent with a contact URL, one
request per *source* rather than per location, a per-host throttle, exponential
backoff on 429/5xx, an on-disk cache for local development, and robots.txt
checks. Login-gated bids (the Valero/Bushel `auth/required` pages) are never
fetched.

Bids are shown as reference only and may be delayed — the map says so, echoing
the disclaimer the co-ops put on their own pages.
