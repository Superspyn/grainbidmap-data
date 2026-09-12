"""How long each truck has been sitting still.

Two sources, because neither alone is right.

**Position history** (`/platform/machines/{principalId}/locationHistory`) is
the authority. It returns every reported position, newest first, and walking
back to the first one more than 60 m away gives the exact moment a truck
arrived where it stands. This is what `refine()` does.

**The ISO 15143-3 fleet feed** is the cheap one: a single call returns every
vehicle's latest position, which is what the five-minute push reads. But its
`<Location>` timestamp is "last position *reported*", not "last *moved*",
and the gap between those is not small. Measured against history:

    16 Mack North        feed said 5 days     actually 12 days
    West Crew            feed said 1 hour     actually 19.5 hours
    Anthony Mack 3       feed said 11 days    actually 20+ days

So the feed alone understates, sometimes by a week. It is used to notice
movement between refinements, never to establish a duration on its own.

On engine idling: there is none to be had for these trucks. The
`hoursOfOperation` API works and is readable - Deere machines come back with
five or six engine-ON periods each - but all 39 road vehicles return a
single engine-OFF period running from the day the tracker was fitted to now,
and `engineHours` reads 0.00. These are position-only aftermarket trackers.
So this measures STOPPED, not idling, and says so. The one exception is kept
for the day a truck does report: operating hours climbing on a vehicle that
has not moved is a running engine.

State lives at %USERPROFILE%\\.grain-map-secrets\\idle-state.json, outside
this public repo, and holds nothing but positions and timestamps.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import pathlib

STATE = pathlib.Path.home() / ".grain-map-secrets" / "idle-state.json"

# A stationary GPS wanders. The most a parked truck here drifted across five
# hours was 23 m, so anything inside 60 m is the same parking spot rather
# than a move. It also means shuffling a truck across the yard does not
# reset the clock, which is the right trade for a hauling map.
JITTER_M = 60.0

# Deere republishes the feed about every 15 minutes, so consecutive pushes
# often re-read one snapshot and a driving truck would appear to stop and
# start. Once seen moving it stays moving for a little longer than one
# republish interval.
MOVING_LATCH_MIN = 20.0

# How far back refine() will look, and how hard it will page. A truck parked
# a fortnight produces a few hundred points, which fits in two or three
# pages of 250.
HISTORY_DAYS = 21
MAX_PAGES = 6

# Re-check a truck whose arrival time is still only a lower bound this often.
# Once history or an observed move has pinned it, it is not looked up again
# until it next moves.
REFINE_EVERY_H = 6.0

# Operating hours climbing while the truck has not moved means the engine is
# running and the truck is not. A hundredth of an hour is the smallest step
# the feed reports.
HOURS_EPSILON = 0.005


def _key(truck: dict) -> str:
    """Identify a vehicle by serial number, falling back to its name.

    Names are editable in Operations Center and several here differ only by
    punctuation, so the serial is the stable one where it is present.
    """
    return str(truck.get("vin") or "").strip() or str(truck.get("name") or "").strip()


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat2 - lat1) * 111_320.0
    dlon = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


def _parse(iso: str | None) -> _dt.datetime | None:
    if not iso:
        return None
    try:
        return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def _minutes_since(iso: str | None, now: _dt.datetime) -> float | None:
    when = _parse(iso)
    return None if when is None else (now - when).total_seconds() / 60.0


def _load() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8")).get("vehicles", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _save(vehicles: dict, now_iso: str) -> None:
    STATE.write_text(json.dumps({"updated": now_iso, "vehicles": vehicles},
                                indent=1), encoding="utf-8")
    try:
        os.chmod(STATE, 0o600)
    except OSError:
        pass


def last_moved(api, token: str, principal_id, lat: float, lon: float,
               days: int = HISTORY_DAYS) -> tuple[str | None, bool]:
    """When the vehicle arrived where it is now, from position history.

    Returns (iso_time, exact). Walks the history newest-first and stops at
    the first point more than JITTER_M from where it stands; the point after
    that - the oldest one still at this spot - is the arrival.

    exact is False when the search ran out of history without finding a
    departure, which only supports "at least this long".
    """
    from jd_fleet import LOCATION_HISTORY     # imported here to avoid a cycle

    now = _dt.datetime.now(_dt.timezone.utc)
    url = LOCATION_HISTORY.format(
        pid=principal_id,
        start=(now - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        end=now.strftime("%Y-%m-%dT%H:%M:%S.000Z"))

    oldest_here = None
    for _ in range(MAX_PAGES):
        status, body = api(token, url)
        if status != 200 or not isinstance(body, dict):
            break
        values = body.get("values") or []
        for point in values:
            p = point.get("point") or {}
            if p.get("lat") is None or p.get("lon") is None:
                continue
            if metres(lat, lon, p["lat"], p["lon"]) > JITTER_M:
                # It was somewhere else at this reading, so it arrived at
                # the reading after this one.
                return oldest_here, True
            oldest_here = point.get("eventTimestamp") or oldest_here
        nxt = [l.get("uri") for l in body.get("links", [])
               if l.get("rel") == "nextPage"]
        if not nxt or not values:
            break
        url = nxt[0]

    # No departure found in the window: it has been here at least as long as
    # the oldest reading we saw, and possibly far longer.
    return oldest_here, False


def refine(api, token: str, trucks: list[dict], index: dict,
           budget: int = 8) -> int:
    """Pin down arrival times from position history, cheapest-first.

    Only trucks whose time is still a lower bound are looked up, at most
    `budget` of them per run, and each at most every REFINE_EVERY_H hours.
    A truck seen to move under observation is already exact and is never
    looked up again until it next moves - so this settles down to nothing
    once the fleet has been watched for a while.
    """
    was = _load()
    now = _dt.datetime.now(_dt.timezone.utc)
    now_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")

    due = []
    for truck in trucks:
        key = _key(truck)
        record = was.get(key)
        if not record or record.get("exact"):
            continue
        if truck.get("lat") is None or truck.get("lon") is None:
            continue
        machine = index.get(str(truck.get("vin") or "").strip())
        if not machine:
            continue
        age = _minutes_since(record.get("refined_at"), now)
        if age is not None and age < REFINE_EVERY_H * 60:
            continue
        due.append((age is None, truck, key, record, machine))

    # Never refined before goes first, so a new truck is pinned down on the
    # run after it appears rather than waiting behind re-checks.
    due.sort(key=lambda d: not d[0])
    done = 0
    for _, truck, key, record, machine in due[:budget]:
        arrived, exact = last_moved(api, token, machine["principal_id"],
                                    truck["lat"], truck["lon"])
        record["refined_at"] = now_iso
        if arrived:
            # History can only ever push the arrival earlier than the feed's
            # own last-report time, never later.
            if not record.get("since") or arrived < record["since"] or exact:
                record["since"] = arrived
            record["exact"] = exact
            record["source"] = "history"
        was[key] = record
        done += 1

    if done:
        _save(was, now_iso)
    return done


def track(trucks: list[dict], persist: bool = True) -> list[dict]:
    """Annotate each truck with how long it has been stopped.

    Adds, per truck:
      since        ISO time it arrived where it is now, best estimate
      since_min    True when `since` is a lower bound, not a known arrival
      moving       True when it has changed position recently
      engine_on    True when operating hours rose while it sat still
    """
    was = _load()
    now = _dt.datetime.now(_dt.timezone.utc)
    now_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    vehicles: dict = {}

    for truck in trucks:
        key = _key(truck)
        lat, lon = truck.get("lat"), truck.get("lon")
        if not key or lat is None or lon is None:
            continue
        at = truck.get("at")
        hours = truck.get("hours")
        prior = was.get(key)

        if prior is None:
            # First sighting. The report time is a lower bound and nothing
            # more - it is when a position was last *reported*, which for a
            # silent tracker is roughly the arrival and for a heartbeating
            # one is not. Guessing from its age was wrong by a week on one
            # truck here, so nothing is called exact until history or an
            # observed move says so.
            record = {"lat": lat, "lon": lon, "since": at or now_iso,
                      "anchor_at": at, "hours": hours, "engine_at": None,
                      "moved_at": None, "refined_at": None,
                      "exact": False, "source": "seed"}
        elif metres(prior["lat"], prior["lon"], lat, lon) > JITTER_M:
            # It moved, under observation. The clock restarts from this
            # report and there is nothing provisional about that - this is
            # the one case that needs no history lookup at all.
            record = {"lat": lat, "lon": lon, "since": at or now_iso,
                      "anchor_at": at, "hours": hours, "engine_at": None,
                      "moved_at": now_iso, "refined_at": None, "exact": True,
                      "source": "observed"}
        else:
            # Same spot. Hold the arrival time; only the evidence changes.
            record = dict(prior)
            record.update({"lat": lat, "lon": lon})
            if (hours is not None and prior.get("hours") is not None
                    and hours - prior["hours"] > HOURS_EPSILON):
                record["engine_at"] = now_iso
            if hours is not None:
                record["hours"] = hours
            # A report time still advancing on the spot means the tracker is
            # heartbeating, so the feed's timestamp is a heartbeat and not
            # an arrival - which disproves the first-sighting guess that it
            # was one. It says nothing against an arrival established by
            # history or by watching the truck move, so those stand.
            if (at and record.get("anchor_at") and at != record["anchor_at"]
                    and record.get("source") == "seed"):
                record["exact"] = False
            record.setdefault("exact", False)
            record.setdefault("source", "seed")

        moved_age = _minutes_since(record.get("moved_at"), now)
        vehicles[key] = record
        truck["since"] = record["since"]
        truck["since_min"] = not record.get("exact")
        truck["moving"] = moved_age is not None and moved_age <= MOVING_LATCH_MIN
        truck["engine_on"] = record.get("engine_at") is not None

    if persist:
        _save(vehicles, now_iso)
    return trucks
