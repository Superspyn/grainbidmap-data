"""Where the combines are and what they are reading, for the moisture calculator.

    python dev/moisture_feed.py              # the scheduled pass, every 30 minutes
    python dev/moisture_feed.py --backfill   # read every field's harvest once
    python dev/moisture_feed.py --dry-run    # show what would be pushed

The calculator page (dev/moisture_build_block.py) needs two things it cannot
get for itself, because Deere's API sends no CORS headers:

  * which field each combine is in right now, so the page can open on it;
  * the combines' moisture readings, so "moisture now" can be filled in -
    from the field itself if it is being harvested, or from the nearest
    field of the same crop harvested in the last three days.

Both come from Operations Center and go to the private relay's /moisture
key. Costs are kept low on purpose, because the same API budget serves the
truck map:

  * positions come from the ISO fleet feed the truck push already reads -
    one request lists every machine;
  * harvest readings are asked for only for fields a combine is in, or was
    in since that field was last read. Each run remembers the fields it has
    seen a combine in, so a field is read again once after the combine
    leaves, to catch the pass's final average. A quiet day costs two
    requests; a busy one a few dozen.

A combine is recognised by model - X9, S7, the S-series and STS - not by its
name, because names are whatever the operator typed.

Every reading is also kept in a history on the PC, which is what a later
calibration of the model against these fields would be built from.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, iso, parse_iso, read_json, write_private  # noqa: E402

STATE = SECRETS / "moisture-state.json"
FLEET = SECRETS / "fleet.json"
RELAY = SECRETS / "relay.json"
USER_AGENT = "grain-map/1.0 (farm hauling map; contact github.com/Superspyn)"

# Deere combine models as the ISO feed spells them: "X9 1100", "S7 800",
# "STS16", and the older S670..S790 and T670 families. STS takes its digits
# inside the group: "STS16" has no word break after "STS", so a bare STS\b
# missed the one this farm actually runs.
COMBINE_MODEL = re.compile(r"^(X9|S7|STS\d*|S[5-9]\d0|T[5-9]\d0|9[5-8][67]0)\b", re.I)

IN_FIELD_HOURS = 3          # a position this recent counts as "in the field now"
REQUERY_MIN = 30            # read a field being harvested at most this often
RECENT_DAYS = 5             # fields a combine left within this are read once more
PUBLISH_DAYS = 14           # readings older than this are not sent to the page
HISTORY_DAYS = 90


# ---------------------------------------------------------------------------
# which field is a point in

def point_in_rings(lat: float, lon: float, rings: list[dict]) -> bool:
    """Even-odd over every ring, so a hole - a farmstead inside a field - is
    outside it. Rings are {"t": "e"|"i", "p": [[lat, lon], ...]}."""
    inside = False
    for ring in rings:
        pts = ring.get("p") or []
        j = len(pts) - 1
        for i in range(len(pts)):
            yi, xi = pts[i]
            yj, xj = pts[j]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def field_bounds(field: dict) -> tuple[float, float, float, float] | None:
    pts = [p for r in field.get("rings") or [] for p in r.get("p") or []]
    if not pts:
        return None
    ys, xs = [p[0] for p in pts], [p[1] for p in pts]
    return min(ys), min(xs), max(ys), max(xs)


def field_at(lat: float, lon: float, fields: list[dict]) -> str | None:
    for f in fields:
        b = f.get("_bounds")
        if b is None or not (b[0] <= lat <= b[2] and b[1] <= lon <= b[3]):
            continue
        if point_in_rings(lat, lon, f["rings"]):
            return f["id"]
    return None


def load_fields() -> list[dict]:
    fields = [f for f in (read_json(FLEET, {}).get("fields") or []) if f.get("rings")]
    for f in fields:
        f["_bounds"] = field_bounds(f)
    return fields


# ---------------------------------------------------------------------------
# Operations Center

def combines(positions: list[dict]) -> list[dict]:
    """The combines out of the ISO feed, with a label that says which machine
    without repeating whatever name the operator gave it."""
    out = []
    for m in positions:
        if m.get("kind") != "equipment" or not COMBINE_MODEL.match(m.get("model") or ""):
            continue
        if m.get("lat") is None or m.get("lon") is None:
            continue
        serial = m.get("vin") or ""
        out.append({"label": f"{m['model']} #{serial[-4:]}" if serial else m["model"],
                    "serial": serial, "lat": m["lat"], "lon": m["lon"], "at": m.get("at")})
    return out


def harvest_reading(api, token: str, field: dict, season: int) -> dict | None:
    """The newest harvest pass on this field this season that has a real
    moisture average. A pass reading 0.0 is a sensor that was not recording -
    two did on this farm in September 2026 - not bone-dry grain, so it is
    skipped rather than published."""
    from jd_fleet import crop_word
    url = (f"/platform/organizations/{field['org']}/fields/{field['id']}"
           f"/fieldOperations?fieldOperationType=HARVEST&cropSeason={season}")
    status, body = api(token, url)
    if status != 200 or not isinstance(body, dict):
        return None
    ops = sorted(body.get("values") or [], key=lambda o: o.get("startDate") or "", reverse=True)
    for op in ops:
        link = next((l["uri"] for l in op.get("links", []) if l.get("rel") == "harvestMoistureResult"), None)
        if not link:
            continue
        _s, m = api(token, link)
        value = ((m or {}).get("averageMoisture") or {}).get("value") if isinstance(m, dict) else None
        if not value:                       # None or 0.0
            continue
        return {"m": round(float(value), 2), "crop": crop_word(op.get("cropName")),
                "start": op.get("startDate"), "end": op.get("endDate") or op.get("startDate")}
    return None


# ---------------------------------------------------------------------------
# the run

def fields_to_read(state: dict, seen: dict[str, str], now: _dt.datetime,
                   all_ids: list[str] | None = None) -> list[str]:
    """Which fields to ask Deere about this run.

    A field a combine is in now is read if it has not been read for
    REQUERY_MIN. A field a combine was in, within RECENT_DAYS, is read once
    more if the combine was seen there after the field was last read - that
    catches the pass's final average after the machine moves on. `all_ids`
    (backfill) reads everything."""
    if all_ids is not None:
        return list(all_ids)
    read_at = state.get("read_at") or {}
    out = []
    for fid, last_seen in (state.get("visits") or {}).items():
        seen_t = parse_iso(last_seen)
        if not seen_t or (now - seen_t).days >= RECENT_DAYS:
            continue
        last_read = parse_iso(read_at.get(fid))
        if fid in seen:
            if not last_read or (now - last_read).total_seconds() >= REQUERY_MIN * 60:
                out.append(fid)
        elif not last_read or last_read < seen_t:
            out.append(fid)
    return out


def payload(state: dict, found: list[dict], now: _dt.datetime) -> dict:
    cutoff = now - _dt.timedelta(days=PUBLISH_DAYS)
    readings = {fid: r for fid, r in (state.get("readings") or {}).items()
                if (parse_iso(r.get("end")) or now) >= cutoff}
    return {"generated_at": iso(now),
            "combines": [{"label": c["label"], "field": c.get("field"), "at": c.get("at"),
                          "y": round(c["lat"], 5), "x": round(c["lon"], 5)} for c in found],
            "readings": readings}


def push(body: dict) -> str | None:
    cfg = read_json(RELAY, {})
    if not cfg.get("url") or not cfg.get("push_token"):
        return None
    req = urllib.request.Request(cfg["url"].rstrip("/") + "/moisture",
                                 data=json.dumps(body).encode(), method="PUT")
    req.add_header("Authorization", "Bearer " + cfg["push_token"])
    req.add_header("Content-Type", "application/json")
    # Cloudflare refuses urllib's default agent before the Worker runs.
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.read().decode()[:120]
    except (urllib.error.URLError, OSError) as exc:
        return f"relay refused: {exc}"


def run(backfill: bool = False, dry_run: bool = False) -> None:
    from jd_fleet import api, fleet_positions, refresh_token
    now = _dt.datetime.now(_dt.timezone.utc)
    state = read_json(STATE, {})
    for key, empty in (("visits", {}), ("read_at", {}), ("readings", {}), ("history", [])):
        state.setdefault(key, empty)
    fields = load_fields()
    by_id = {f["id"]: f for f in fields}
    token = refresh_token()

    found = combines(fleet_positions(token))
    seen: dict[str, str] = {}
    for c in found:
        t = parse_iso(c.get("at"))
        fresh = t is not None and (now - t).total_seconds() < IN_FIELD_HOURS * 3600
        c["field"] = field_at(c["lat"], c["lon"], fields) if fresh else None
        if c["field"]:
            seen[c["field"]] = max(seen.get(c["field"], ""), c["at"])
            state["visits"][c["field"]] = max(state["visits"].get(c["field"], ""), c["at"])
    print(f"{len(found)} combines in the feed, {sum(1 for c in found if c['field'])} in a field now")

    todo = fields_to_read(state, seen, now, list(by_id) if backfill else None)
    changed = 0
    for n, fid in enumerate(todo, 1):
        f = by_id.get(fid)
        if not f:
            continue
        r = harvest_reading(api, token, f, now.year)
        state["read_at"][fid] = iso(now)
        if r:
            if state["readings"].get(fid) != r:
                changed += 1
                state["history"].append(dict(r, field=fid, read=iso(now)))
            state["readings"][fid] = r
        if backfill:
            time.sleep(0.15)
            if n % 50 == 0:
                print(f"  {n}/{len(todo)} fields read, {len(state['readings'])} with a reading")
    print(f"read {len(todo)} field(s), {changed} new or changed reading(s), "
          f"{len(state['readings'])} fields with a reading this season")

    horizon = now - _dt.timedelta(days=HISTORY_DAYS)
    state["history"] = [h for h in state["history"] if (parse_iso(h.get("read")) or now) >= horizon]
    body = payload(state, found, now)
    if dry_run:
        print(json.dumps({"combines": body["combines"], "readings": len(body["readings"])}, indent=1)[:1500])
        return
    result = push(body)
    if result:
        print(f"  relay: {result}")
    write_private(STATE, state, separators=(",", ":"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true", help="read every field's harvest once")
    ap.add_argument("--dry-run", action="store_true", help="do not push or save")
    args = ap.parse_args()
    run(backfill=args.backfill, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
