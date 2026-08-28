"""Adapter for Bushel's markets-aggregator API.

CHS's public cash-bids page (chsag.com) loads its data from

    POST api.bushelpowered.com/api/markets/aggregator/bids/v1/GetBidsList

with the co-op's public slug in an ``App-Company`` header and a literal ``{}``
body. No credentials: the page embeds the slug in plain markup, and the
component's own code treats its token property as optional. One request
returns every location the co-op quotes.

Compliance note, because Bushel needs one: this API host serves NO robots.txt
(404 - no stated policy), unlike Bushel's hosted-site domains
(``*.bushelsites.com``, ``smithfieldgrain.com``...) which explicitly disallow
all non-search crawlers. Those policies govern their own pages; robots.txt is
per-origin. The user decided to use the API on that reading - the same call as
Cargill, minus the explicit allow. Back it out by removing the bushel sources
from sources.yaml. Login-gated Bushel portals (portal.bushelpowered.com,
Valero) remain untouched.

Shape notes:
  * A location is {id, name, groups[]}; a group is one commodity holding
    bids[]. No coordinates, so pin matching is by name.
  * ``description`` is the delivery label as free text - "AUG 2026",
    "O/N 2026", "New Crop 2026", "Open Storage". The parseable shapes become
    delivery dates; the rest keep their label with null dates.
  * ``futuresChangeSign`` is an explicit 1 / -1 next to an unsigned-looking
    ``futuresChange``. Every observed row is sign=1, so - like Nexus - the
    negative branch is handled but has never been seen live: a sign already in
    the text wins, otherwise sign=-1 negates.
  * bidType "cash" carries the full row; "flat" has a price but no futures
    (kept, with null futures fields); "floating" is basis-only (dropped - the
    map needs a cash price).
"""

from __future__ import annotations

import datetime as _dt
import json
import re

import fetch
import normalize
from adapters.base import Bid, SourceLocation

URL = "https://api.bushelpowered.com/api/markets/aggregator/bids/v1/GetBidsList"

_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_MONTH_ALIASES = {m[:3]: i + 1 for i, m in enumerate(_MONTHS)}
_MONTH_ALIASES.update({"sept": 9, "july": 7, "june": 6, "febuary": 2})

# "AUG 2026", "Sept 2026", "OCT/NOV 2026", "O/N 2026", "March 2027"
_LABEL_RE = re.compile(
    r"^\s*(?:nc\s+)?([a-z]+)(?:\s*/\s*([a-z]+))?\.?\s+(\d{4}|\d{2})\s*$", re.I
)
# Month INITIALS, as used in labels like "O/N 2026" - NOT futures month codes
# (in futures notation N is July; in "O/N" it is November). Ambiguous initials
# list every month they could mean; a pair is resolved by taking the second
# month as the nearest one after the first.
_INITIALS = {"j": [1, 6, 7], "f": [2], "m": [3, 5], "a": [4, 8],
             "s": [9], "o": [10], "n": [11], "d": [12]}


def _month_number(word: str) -> int | None:
    w = word.lower().rstrip(".")
    if w in _MONTH_ALIASES:
        return _MONTH_ALIASES[w]
    for full, i in ((m, i + 1) for i, m in enumerate(
            ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"])):
        if w == full:
            return i
    return None


def _letter_pair(first: str, second: str) -> tuple[int, int] | None:
    """Resolve "O/N"-style initial pairs to the closest forward month pair."""
    a = _INITIALS.get(first.lower())
    b = _INITIALS.get(second.lower())
    if not a or not b:
        return None
    best = None
    for m1 in a:
        for m2 in b:
            gap = (m2 - m1) % 12 or 12
            if best is None or gap < best[0]:
                best = (gap, m1, m2)
    return (best[1], best[2]) if best else None


def parse_delivery(label: str) -> tuple[str | None, str | None]:
    """Turn Bushel's free-text delivery label into a (start, end) window.

    Anything unrecognised - "New Crop 2026", "Open Storage", a bare month with
    no year - returns (None, None) and the label is shown as-is.
    """
    m = _LABEL_RE.match(label or "")
    if not m:
        return None, None
    first, second, year = m.groups()
    y = int(year)
    if y < 100:
        y += 2000
    if second and len(first) == 1 and len(second) == 1:
        pair = _letter_pair(first, second)
        if pair is None:
            return None, None
        m1, m2 = pair
    else:
        m1 = _month_number(first)
        m2 = _month_number(second) if second else m1
    if m1 is None or m2 is None:
        return None, None
    start, _ = normalize.month_bounds(f"{_dt.date(y, m1, 1):%b %Y}")
    end_year = y + 1 if m2 < m1 else y
    _, end = normalize.month_bounds(f"{_dt.date(end_year, m2, 1):%b %Y}")
    return start, end


class BushelAdapter:
    def __init__(self, slug: str, label: str, referer: str | None = None):
        self.slug = slug
        self.label = label
        self.referer = referer
        self.name = f"bushel:{slug}"

    def fetch(self) -> list[SourceLocation]:
        body = fetch.get(
            URL,
            referer=self.referer,
            impersonate=True,
            method="POST",
            extra_headers={"App-Company": self.slug,
                           "Content-Type": "application/json"},
            body="{}",
        )
        return self.parse(body, self.slug)

    @classmethod
    def parse(cls, body: str, slug: str) -> list[SourceLocation]:
        payload = json.loads(body)
        as_of = _now()

        locations: list[SourceLocation] = []
        for raw in payload.get("locations", []):
            bids: list[Bid] = []
            for group in raw.get("groups", []):
                grain = normalize.classify_grain(
                    (group.get("commodity") or {}).get("name"),
                    group.get("displayName"),
                )
                if grain is None:
                    continue
                for row in group.get("bids", []):
                    bid = cls._parse_bid(row, grain)
                    if bid is not None:
                        bids.append(bid)
            if not bids:
                continue
            bids.sort(key=lambda b: (b.grain, b.delivery_start or "9999-99-99"))
            locations.append(
                SourceLocation(
                    source_location_id=str(raw.get("id") or raw.get("name")),
                    name=(raw.get("name") or "").strip(),
                    bids=bids,
                    as_of=as_of,
                )
            )

        if not locations:
            raise ValueError(f"bushel:{slug}: no corn or soybean bids found")
        return locations

    @staticmethod
    def _parse_bid(row: dict, grain: str) -> Bid | None:
        if row.get("bidType") == "floating":
            return None  # basis-only, no cash price to show
        cash = normalize.parse_money(row.get("bidPrice"))
        # Flat rows sometimes carry a literal "0.00" - a placeholder, not a
        # zero-dollar bid. Publishing it would feed $0.00 into net-price math.
        if cash is None or cash <= 0:
            return None

        futures = normalize.parse_money(row.get("futuresPrice"))
        basis = normalize.parse_money(row.get("basisPrice"))
        change = normalize.parse_money(row.get("futuresChange"))
        # The sign arrives out-of-band. A sign already present in the text
        # wins; otherwise futuresChangeSign == -1 negates. Every observed row
        # is sign=1, so the negative branch is untested against live data.
        sign = row.get("futuresChangeSign")
        if (change is not None and change > 0 and sign == -1
                and not str(row.get("futuresChange", "")).strip().startswith(("-", "+"))):
            change = -change

        label = (row.get("description") or "").strip()
        start, end = parse_delivery(label)

        return Bid(
            grain=grain,
            delivery_start=start,
            delivery_end=end,
            delivery_label=label or normalize.format_delivery_label(start, end),
            futures_month=normalize.futures_month_from_symbol(row.get("futuresSymbol")),
            futures=futures,
            futures_change=change,
            basis=basis,
            cash=cash,
        )


def _now() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
