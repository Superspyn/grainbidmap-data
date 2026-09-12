"""Where each truck has been, and where it sat still for too long.

Breadcrumbs (`/platform/machines/{principalId}/breadcrumbs`) are the richest
thing Deere gives for these trucks: a point every few seconds while the
tracker is awake, each carrying **speed**. That is what makes both features
here possible, and neither is possible from the ISO fleet feed.

Two things come out of one fetch:

* the **trail** - the path driven, for the map to draw
* **long stops** - a run of near-zero-speed points, which is as close to
  "idling" as this fleet's hardware allows

Why a stop is not simply "speed was zero for ten minutes", measured over
seven days of real breadcrumbs:

    gap to the next breadcrumb, while moving    median 14 s, p90 60 s
    gap to the next breadcrumb, while stopped   median 27 s, p90 508 s

While the tracker is awake it reports every half-minute or so whether or not
the truck is rolling. When the truck is parked the tracker sleeps and the
gap stretches to hours. Treating one long sleep as a single stationary run
called overnight parking a 72-hour idle. So a run is broken whenever the gap
exceeds GAP_MIN, which leaves only stretches where the tracker was awake and
reporting steadily and the truck was not moving. Over the same seven days
that is 6 stops of 10 minutes or more - about one a day - rather than 51.

This still cannot prove the engine was running: Deere holds no engine data
for these trucks (see jd_idle). A truck reporting every 27 seconds while
stationary is almost certainly running, but the wording on the map says
"stopped", not "idling", because that is what was actually measured.

State lives at %USERPROFILE%\\.grain-map-secrets\\trails.json, outside this
public repo.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib

STATE = pathlib.Path.home() / ".grain-map-secrets" / "trails.json"

# How much of the path to keep and show.
TRAIL_HOURS = 24

# Below this the truck is not moving. Breadcrumb speed is km/h, and a
# stationary GPS reports a few tenths.
MOVING_KMH = 1.5

# Break a stationary run when the tracker goes quiet for longer than this:
# past it, the silence is a sleeping tracker rather than a waiting truck.
GAP_MIN = 5.0

# Report a stop at least this long. The farmer asked for ten minutes.
IDLE_MIN = 10.0

# Keep stops for a week, and never let the file grow without bound.
EVENT_DAYS = 7
MAX_EVENTS = 400

# Only ask for breadcrumbs from trucks that have reported recently. A truck
# parked for a fortnight has none to give, and asking 37 times every five
# minutes is a lot of requests for nothing.
FETCH_IF_REPORTED_WITHIN_H = TRAIL_HOURS

# A truck that is moving is worth re-reading every push; one standing still
# is not, and this is what keeps the request count sane - a few moving
# vehicles per push instead of twenty.
STOPPED_REFRESH_MIN = 30.0


def _parse(iso: str | None) -> _dt.datetime | None:
    if not iso:
        return None
    try:
        return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(when: _dt.datetime) -> str:
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def _load() -> dict:
    if not STATE.exists():
        return {"vehicles": {}, "events": []}
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        data.setdefault("vehicles", {})
        data.setdefault("events", [])
        return data
    except (json.JSONDecodeError, OSError):
        return {"vehicles": {}, "events": []}


def _save(data: dict) -> None:
    STATE.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    try:
        os.chmod(STATE, 0o600)
    except OSError:
        pass


def _key(truck: dict) -> str:
    return str(truck.get("vin") or "").strip() or str(truck.get("name") or "").strip()


def fetch_breadcrumbs(api, token: str, principal_id, since: _dt.datetime,
                      until: _dt.datetime, max_pages: int = 8) -> list[dict]:
    """Breadcrumbs in a window, oldest first, following nextPage."""
    url = (f"https://api.deere.com/platform/machines/{principal_id}/breadcrumbs"
           f"?startDate={_iso(since).replace('Z', '.000Z')}"
           f"&endDate={_iso(until).replace('Z', '.000Z')}&itemLimit=250")
    out: list[dict] = []
    for _ in range(max_pages):
        status, body = api(token, url)
        if status != 200 or not isinstance(body, dict):
            break
        values = body.get("values") or []
        out += values
        nxt = [l.get("uri") for l in body.get("links", [])
               if l.get("rel") == "nextPage"]
        if not nxt or not values:
            break
        url = nxt[0]
    rows = []
    for p in out:
        point = p.get("point") or {}
        ts = p.get("eventTimestamp")
        if point.get("lat") is None or point.get("lon") is None or not ts:
            continue
        rows.append({"t": ts,
                     "y": round(float(point["lat"]), 5),
                     "x": round(float(point["lon"]), 5),
                     "s": round(float((p.get("speed") or {})
                                      .get("valueAsDouble") or 0.0), 1)})
    rows.sort(key=lambda r: r["t"])
    return rows


def find_stops(points: list[dict]) -> list[dict]:
    """Stretches of at least IDLE_MIN where the truck was not moving and the
    tracker was still reporting steadily."""
    stops, run = [], []

    def close(run):
        if len(run) < 2:
            return
        start, end = _parse(run[0]["t"]), _parse(run[-1]["t"])
        if start and end and (end - start).total_seconds() / 60 >= IDLE_MIN:
            stops.append({"start": run[0]["t"], "end": run[-1]["t"],
                          "minutes": round((end - start).total_seconds() / 60),
                          "y": run[-1]["y"], "x": run[-1]["x"]})

    for p in points:
        moving = p["s"] > MOVING_KMH
        gap_too_big = False
        if run:
            previous, current = _parse(run[-1]["t"]), _parse(p["t"])
            if previous and current:
                gap_too_big = (current - previous).total_seconds() / 60 > GAP_MIN
        if moving or gap_too_big:
            close(run)
            run = [] if moving else [p]
        else:
            run.append(p)
    close(run)
    return stops


def update(api, token: str, trucks: list[dict], index: dict,
           budget: int = 20) -> tuple[int, list[dict]]:
    """Refresh trails and long stops. Returns (trucks fetched, new stops).

    Each truck is asked only for breadcrumbs newer than the last one already
    held, so a busy truck costs one small request per push and a parked one
    costs nothing at all.
    """
    data = _load()
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(hours=TRAIL_HOURS)
    seen = {e["id"] + e["start"] for e in data["events"]}
    fetched = 0
    new_stops: list[dict] = []

    for truck in trucks:
        key = _key(truck)
        machine = index.get(str(truck.get("vin") or "").strip())
        if not key or not machine:
            continue
        reported = _parse(truck.get("at"))
        record = data["vehicles"].get(key) or {"points": [], "to": None,
                                               "checked": None}
        stale = (reported is None
                 or (now - reported).total_seconds() / 3600 > FETCH_IF_REPORTED_WITHIN_H)
        if stale and not record["points"]:
            continue                       # parked for days, nothing to draw
        checked = _parse(record.get("checked"))
        if (not truck.get("moving") and checked is not None
                and (now - checked).total_seconds() / 60 < STOPPED_REFRESH_MIN):
            continue                       # standing still; its path has not changed
        if fetched >= budget:
            continue
        record["checked"] = _iso(now)

        since = _parse(record.get("to")) or cutoff
        since = max(since, cutoff)
        rows = fetch_breadcrumbs(api, token, machine["principal_id"], since, now)
        fetched += 1

        merged = {p["t"]: p for p in record["points"]}
        merged.update({p["t"]: p for p in rows})
        points = [p for p in sorted(merged.values(), key=lambda r: r["t"])
                  if (_parse(p["t"]) or now) >= cutoff]
        record["points"] = points
        record["to"] = points[-1]["t"] if points else _iso(now)
        data["vehicles"][key] = record

        for stop in find_stops(points):
            marker = key + stop["start"]
            if marker in seen:
                continue
            seen.add(marker)
            event = {"id": key, "name": truck.get("name"),
                     "kind": truck.get("kind"), **stop}
            data["events"].append(event)
            new_stops.append(event)

    horizon = now - _dt.timedelta(days=EVENT_DAYS)
    data["events"] = [e for e in data["events"]
                      if (_parse(e.get("end")) or now) >= horizon][-MAX_EVENTS:]
    data["events"].sort(key=lambda e: e.get("end") or "", reverse=True)
    _save(data)

    for truck in trucks:
        record = data["vehicles"].get(_key(truck))
        truck["trail"] = [[p["y"], p["x"]] for p in record["points"]] if record else []
    return fetched, new_stops


def recent_events(hours: int = 24) -> list[dict]:
    data = _load()
    horizon = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
    return [e for e in data["events"] if (_parse(e.get("end")) or horizon) >= horizon]
