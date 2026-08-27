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
git remote add origin https://github.com/Superspyn/grainbidmap-data.git
git push -u origin main
```

### 2. Turn on GitHub Pages

Repo **Settings → Pages → Source: Deploy from a branch**, branch `main`,
folder `/docs`. Your feed will then be at:

```
https://superspyn.github.io/grainbidmap-data/bids.json
```

Pages serves `Access-Control-Allow-Origin: *`, which is what lets the map fetch
it from Squarespace.

### 3. The map already points at that URL

`grain-trucking-map.html` line 761 is already set:

```js
var GT_BIDS_URL = 'https://superspyn.github.io/grainbidmap-data/bids.json';
```

Change it only if the repo is renamed. Note the Pages hostname lowercases the
username. If the feed is ever unreachable the map simply skips it and behaves
exactly as it did before — manual bid entry, "View bid page" links.

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
single request. Landus is the exception - it has no bulk endpoint, so it costs
1 + 51 requests while every other source costs one.

| Source | Pins | How |
|---|---|---|
| NEW Cooperative | 70 | Own pages (TLS impersonation) |
| Heartland Co-op | 53 | Server-rendered bid sheet |
| Landus Cooperative | 45 | Own JSON API (per location) |
| CVA | 37 | AgriCharts |
| Gold-Eagle Co-op | 24 | AgriCharts |
| CGB | 16 | AgriCharts |
| POET Biorefining | 16 | Gradable API |
| Key Cooperative | 16 | AgriCharts |
| Pro Co-op | 14 | AgriCharts |
| Stateline Cooperative | 9 | AgriCharts |
| ADM | 8 | Gradable API |
| North Iowa Co-op | 4 | AgriCharts |
| Two Rivers Cooperative | 4 | AgriCharts |
| Tama-Benton Cooperative | 3 | AgriCharts |
| Innovative Ag | 2 | AgriCharts |
| JBS Live Pork | 2 | AgriCharts |
| Mid-Iowa Milling | 2 | AgriCharts |
| CFE | 1 | AgriCharts |
| SilverEdge Cooperative | 1 | AgriCharts |

**327 of 722 pins** carry live bids (4083 bid rows). The rest:

- **341 pins are on companies with no adapter yet.** Biggest: MFA (59), Cargill (31), Nexus Cooperative (18), AGP (16), River Valley Cooperative (9), Valero (7).
- **49 pins matched a source that publishes no bid for them** — out-of-state and
  seasonal facilities their co-op does not quote.
- **5 pins are held back** as low-confidence or ambiguous matches. One of
  them, `Landus Davis City`, is a pin for an elevator Landus has since sold; the
  `New Coop Davis City` pin covers the same site and does carry bids.

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

### Sources we deliberately do not scrape

**Cargill.** The adapter exists and works (`scrapers/adapters/cargill.py`), but
its source entry is commented out. Cargill's cash bids come from
`d96y3rjfk5o7l.cloudfront.net`, whose robots.txt is served correctly as HTTP 200
and says `User-agent: * / Disallow: /`. `cargillag.com` itself allows crawling,
but its location pages pull the bids from that same CDN, so there is no
compliant route. Do not enable it without Cargill's permission.

Note the difference from the case below: a bot wall that *breaks* the robots
check is worth fixing, an explicit `Disallow` is not worth working around.

### Bot-protected sources

Some sites reject Python's default TLS handshake with a 403 while serving the
same public page to a browser. Pass `impersonate=True` to `fetch.get()` and it
replays a real browser's TLS fingerprint via `curl_cffi`. NEW Co-op is the only
source that currently needs it.

`robots.txt` is fetched with the same client for those hosts. That matters:
`RobotFileParser.read()` uses plain urllib, gets the same 403, and the standard
says a 403 on robots.txt means "disallow everything" — so a bot-protected site
would look like it forbids all crawling when it does not. NEW Co-op's robots.txt
explicitly allows `/cash-bids/`.

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
