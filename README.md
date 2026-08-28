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

### 5. The farm PC does the scraping

All routine scraping runs as a Windows scheduled task on the farm PC —
weekdays every 30 minutes (:15 and :45), 8:15am–5:45pm local time, which
follows daylight saving on its own. Install it once:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1
```

It logs to `.build/update-bids.log` and needs no administrator rights. Remove it
with `Unregister-ScheduledTask -TaskName 'Update cash bids' -Confirm:$false`.

GitHub Actions used to run on a schedule as a backup, and doesn't anymore — a
deliberate choice, for two reasons observed in practice:

- **NEW Co-op and Landus block GitHub's datacenter IP ranges** — 403 and 429
  respectively — while answering normally from a home connection seconds later.
  `curl_cffi` does install and run on the runner, so this is IP reputation, not
  TLS fingerprinting. A cloud run can never refresh those 116 pins. Working
  around it would mean proxying through a residential address, which is evading
  an access control they put up deliberately; running from an actual home
  connection is the honest fix.
- **GitHub's cron was unreliable anyway**: 1 of 10 scheduled slots actually
  fired, and that one ran 2.5 hours late. The farm PC task went 8 for 8.

The workflow (`.github/workflows/bids.yml`) is kept as a **manual fallback**:
if the PC is off for days, trigger it from the **Actions** tab → *Update cash
bids* → *Run workflow*. It refreshes the 19 sources it can reach and flags NEW
Co-op and Landus `"stale": true` with their last good bids — never blanked.

Both push to `main`. `bids.json` is generated rather than authored, so a
collision needs no merge — whichever build has the later `generated_at` wins,
and both sides settle it the same way.

**If the map's bids ever look frozen**, check in this order: is the PC on and
were you logged in? Task Scheduler → "Update cash bids" → Last Run Result.
Then `.build/update-bids.log` in the repo.

---

## What's covered

Bids come from platform endpoints that return every location for a co-op in a
single request. Landus is the exception - it has no bulk endpoint, so it costs
1 + 51 requests while every other source costs one.

| Source | Pins | How |
|---|---|---|
| NEW Cooperative | 70 | Own pages (TLS impersonation) |
| Heartland Co-op | 53 | Server-rendered bid sheet |
| Landus Cooperative | 46 | Own JSON API (per location) |
| CVA | 34 | AgriCharts |
| Cargill | 27 | AgriCharts (`cargillus` — see below) |
| Gold-Eagle Co-op | 24 | AgriCharts |
| CGB | 16 | AgriCharts |
| Key Cooperative | 16 | AgriCharts |
| Nexus Cooperative | 16 | Own server-rendered page |
| POET Biorefining | 15 | Gradable API |
| Pro Co-op | 14 | AgriCharts |
| Stateline Cooperative | 9 | AgriCharts |
| ADM | 8 | Gradable API |
| Two Rivers Cooperative | 4 | AgriCharts |
| North Iowa Co-op | 3 | AgriCharts |
| Tama-Benton Cooperative | 3 | AgriCharts |
| Innovative Ag | 2 | AgriCharts |
| JBS Live Pork | 2 | AgriCharts |
| Mid-Iowa Milling | 2 | AgriCharts |
| CFE | 1 | AgriCharts |
| Golden Grain Energy | 1 | CI Hedging widget API |
| SilverEdge Cooperative | 1 | AgriCharts |

**367 of 722 pins** carry live bids (~4,850 bid rows). The rest:

- **291 pins are on companies with no adapter yet.** Biggest: MFA (59), AGP (16), River Valley Cooperative (9), CHS (7), Valero (7).
- **59 pins matched a source that publishes no bid for them** — out-of-state and
  seasonal facilities their co-op does not quote, plus pins whose co-op only
  quotes a *delivered* bid to someone else's plant (see below).
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

### Cargill, and reading robots.txt per host

Cargill was disabled here for a while, on a reading that turned out to be
incomplete. The note said there was "no compliant route" to their bids. There
is one, and it is worth writing down why.

Cargill's own site loads bids from `d96y3rjfk5o7l.cloudfront.net`, which 301s to
`cargillus.websol.barchart.com`. That host says `User-agent: * / Disallow: /`.
But the same tenant is also served from `cargillus.agricharts.com`, whose
robots.txt — identical to the one every other AgriCharts tenant here serves —
disallows only `/markets/`. We request `/inc/cashbids/cashbids-js.php`, so the
host we actually talk to permits the request we actually make.

Both hosts are Barchart's. The vendor publishes two different policies, and
robots.txt is per-origin by definition, so the permissive one governs. This is
the same platform and endpoint as the fourteen other AgriCharts sources, all of
which pass the identical check in `fetch.py`.

That reasoning is defensible, but it does rest on the per-origin reading, and
the stricter sibling host is a real signal about intent. Enabling it was a
deliberate decision, not a default. To back it out, comment the block out in
`sources.yaml`; the 28 pins fall back to their "View bid page" link.

`fetch.py` still refuses the strict host, so the adapter cannot be repointed at
it by accident:

```
_robots_allows("https://cargillus.agricharts.com/inc/cashbids/...")  -> True
_robots_allows("https://cargillus.agricharts.com/markets/chart.php") -> False
_robots_allows("https://cargillus.websol.barchart.com/")             -> False
```

The general rule this leaves: a bot wall that *breaks* the robots check is worth
fixing, and an explicit `Disallow` on the host you are talking to is not worth
working around.

### Delivered bids to someone else's plant

Co-ops quote bids for grain delivered to other companies' plants. Nexus
publishes `Valero Charles City, IA`, `AGP Manning, IA` and `Cargill Iowa
Falls, IA` next to its own elevators; Heartland publishes `CARGILL - BLAIR`
and `POET - JEWELL`; CVA publishes `ADM Columbus`.

Those are real bids, but they price delivery to a *different building*. Nexus's
own Charles City elevator is 6 km from Valero's plant, so attaching that bid to
the Nexus pin quoted a haul to the wrong place — and CVA Columbus NE was
already showing CVA's delivered bid to ADM Columbus before Nexus was added.

`names_another_company()` in `match_locations.py` drops these before scoring.
Candidates only ever come from the pin's own co-op feed, so a location inside
that feed carrying a different company's name is a delivered bid by definition.
Two guards stop it firing on ordinary town names: the candidate must **lead**
with the company name (every delivered bid here is written that way), and
generic words are stripped first — `Corn LP` reduces to `corn`, which would
otherwise flag `Clinton, IA (Corn Processing)`, and `New Coop` reduces to `new`,
which would flag Cargill's own `New Madrid`.

The run prints how many it ignored. A pin whose co-op only quotes a delivered
bid now correctly carries **no** bid rather than a wrong one.

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

### Overriding a match by hand

`scrapers/config/manual_matches.json` maps a pin id straight to a source
location, wins over automatic matching, and publishes at full confidence. Use it
when the matcher cannot succeed on its own — a pin named `Landus coop` carries no
town, and Landus publishes no coordinates, so neither name nor geography can
find it (that pin is Landus's Britt elevator).

Entries are validated on every run: an unknown source, a location id the source
did not return, or a key matching no pin all print a warning and are ignored
rather than silently publishing something wrong. Renaming a pin changes its id
and orphans its override — the warning is how you find out.

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
