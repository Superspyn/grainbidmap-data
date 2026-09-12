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
the truck is rolling. When the truck is parked the tracker sleeps and the gap
stretches to hours.

That sleep is the whole difficulty, because it is ambiguous on its own: the
truck either sat where it was, or drove somewhere unobserved and came back.
Breaking the run on every long gap resolved it the wrong way - it discarded
the sleep, and the sleep IS the stop, so no stop was ever reported at all.
Asking instead whether the truck MOVED across the gap resolves it correctly,
and the data separates cleanly. Over a day of real breadcrumbs:

    gap where the truck stayed put      moved 8 m, 9 m, 18 m     -> a stop
    gap where the truck went somewhere  moved 73 m ... 2,041 m   -> not a stop

So a run survives a quiet stretch when the truck is in the same place on
either side of it, and breaks when it is not. Overnight parking then reports
as one long stop rather than as nothing, which is the honest answer.

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
import math
import os
import pathlib

STATE = pathlib.Path.home() / ".grain-map-secrets" / "trails.json"

# How much of the path to keep and show.
TRAIL_HOURS = 24

# Breadcrumb speed is km/h. It is kept for drawing the trail, but it is NOT
# what decides whether the truck is moving, because on this hardware it
# disagrees with the ground: two of the three real stops in a day of data
# carry 2.4 km/h on the last point before the truck sat still for twenty
# minutes and travelled eight metres. Displacement is the measurement;
# reported speed is a derived number that can be wrong.
MOVING_KMH = 1.5

# Past this the tracker has gone quiet rather than merely reported slowly,
# and the question becomes whether the truck moved while it slept.
GAP_MIN = 5.0

# How far a parked truck's GPS wanders. Measured across every quiet stretch
# in a day of breadcrumbs: the ones where the truck stayed put came back 8 to
# 18 m away, the nearest one that had actually driven was 73 m. Anywhere in
# that gap works; 60 m sits in it with room on both sides.
SAME_SPOT_M = 60.0

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


def _metres(a: dict, b: dict) -> float:
    r = 6371008.8
    p1, p2 = math.radians(a["y"]), math.radians(b["y"])
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(b["x"] - a["x"]) / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def find_stops(points: list[dict]) -> list[dict]:
    """Stretches of at least IDLE_MIN where the truck did not move.

    A quiet stretch only ends the stop if the truck is somewhere else when
    the tracker wakes up. If it is in the same place, it sat there the whole
    time and the silence counts towards the stop - which is what makes a
    parked truck reportable at all, since a parked truck stops reporting.
    """
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
        if not run:
            run = [p]
            continue
        # Did it end up somewhere else? That is the whole test, and it reads
        # the same whether the previous breadcrumb was forty seconds ago or
        # seven hours ago - which is why the tracker's sleep stops mattering.
        if _metres(run[-1], p) > SAME_SPOT_M:
            close(run)
            run = [p]
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
