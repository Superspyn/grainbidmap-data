"""How long each truck has been sitting still.

Deere's fleet feed cannot answer this on its own, which is worth stating
plainly because it looks like it should. Measured against this account:

  * No road vehicle carries CumulativeIdleHours. Of 99 machines in the feed
    32 do, and they are all Deere farm equipment - none of the 37 trucks.
  * CumulativeOperatingHours is present on all 37 but reads 0.00 on 30 of
    them. These are aftermarket trackers, not engine ECUs.
  * The <Location> timestamp is "when a position was last reported", not
    "when it last moved". Over one five-hour window, 33 of 37 trucks had a
    frozen timestamp - the tracker sleeps while parked - and 4 kept
    heartbeating hourly with 0-23 m of GPS wander.

So the duration has to be accumulated by watching. This module keeps a small
state file beside the credentials and is called on every push, every five
minutes, which is what turns a series of snapshots into "stopped 3 h ago".

For a sleeping tracker the answer is exact from the first run, because a
frozen timestamp already says when the truck stopped. For a heartbeater the
first run can only say "at least this long", which is flagged rather than
presented as fact and becomes exact once it next moves under observation.

This measures STOPPED, not engine idling: with no engine data on these
trucks the two cannot be told apart, except for the few that report
operating hours, where hours climbing on a truck that has not moved does
mean the engine is running.

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

# On a first sighting a recent position report is ambiguous: the truck may
# have just arrived, or be a heartbeater that parked days ago. Older than
# this and no heartbeat can explain it, so it is an arrival time.
SEED_TRUST_MIN = 120.0

# Operating hours climbing while the truck has not moved means the engine is
# running and the truck is not: idling, in the sense Operations Center means
# it. A hundredth of an hour is the smallest step the feed reports.
HOURS_EPSILON = 0.005


def _key(truck: dict) -> str:
    """Identify a vehicle by serial number, falling back to its name.

    Names are editable in Operations Center and several here differ only by
    punctuation, so the serial is the stable one where it is present.
    """
    return str(truck.get("vin") or "").strip() or str(truck.get("name") or "").strip()


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
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
            # First sighting. The position report time is the best evidence
            # available of when it got there, and its age says how far to
            # trust that: an old report can only mean a sleeping tracker.
            age = _minutes_since(at, now)
            record = {"lat": lat, "lon": lon, "since": at or now_iso,
                      "anchor_at": at, "hours": hours, "engine_at": None,
                      "moved_at": None,
                      "since_min": age is None or age < SEED_TRUST_MIN}
        elif _metres(prior["lat"], prior["lon"], lat, lon) > JITTER_M:
            # It moved, under observation. The clock restarts from this
            # report, and there is nothing provisional about that.
            record = {"lat": lat, "lon": lon, "since": at or now_iso,
                      "anchor_at": at, "hours": hours, "engine_at": None,
                      "moved_at": now_iso, "since_min": False}
        else:
            # Same spot. Hold the arrival time; only the evidence changes.
            record = dict(prior)
            record.update({"lat": lat, "lon": lon})
            if (hours is not None and prior.get("hours") is not None
                    and hours - prior["hours"] > HOURS_EPSILON):
                record["engine_at"] = now_iso
            if hours is not None:
                record["hours"] = hours
            # A report time that keeps advancing on the spot is the
            # heartbeating case: this truck was parked before we started
            # watching, so the arrival time stays a lower bound.
            if at and record.get("anchor_at") and at != record["anchor_at"]:
                record["since_min"] = True
            record.setdefault("since_min", False)

        moved_age = _minutes_since(record.get("moved_at"), now)
        vehicles[key] = record
        truck["since"] = record["since"]
        truck["since_min"] = bool(record.get("since_min"))
        truck["moving"] = moved_age is not None and moved_age <= MOVING_LATCH_MIN
        truck["engine_on"] = record.get("engine_at") is not None

    if persist:
        _save(vehicles, now_iso)
    return trucks
