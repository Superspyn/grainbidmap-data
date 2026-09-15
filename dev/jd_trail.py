"""Where each truck has been, and where it sat still for too long.

Breadcrumbs (`/platform/machines/{principalId}/breadcrumbs`) are the richest
thing Deere gives for these trucks: a point every few seconds while the
tracker is awake, each carrying **speed**. That is what makes both features
here possible, and neither is possible from the ISO fleet feed.

Two things come out of one fetch:

* the **trail** - the path driven, for the map to draw
* **long stops** - where the truck stood still, each one classified as
  idling or parked from the engine's own voltage

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

A stop is not yet idling, and the difference is the whole point: a truck
with the key out is not burning anything. It was believed this could not be
told apart, because `engineHours` is frozen (6,220 readings for one truck,
every one of them 0.9) and `hoursOfOperation` returns a single engine-off
period since the tracker was fitted. Both of those are indeed dead.

The engine is in `deviceStateReports`, as `batteryVoltage`. An alternator
holds the system at 13.2-14.5 V; a battery on its own sits at 12.3-12.9 V;
cranking dips to 11.8 V. The two populations do not overlap, and shutdown
and restart are timestamped to the second, so engine-on periods reconstruct
exactly and `idling` means stationary while one of them was running.

Do not substitute `engineState` for this. Deere sets it to 1 on a start and
leaves it 0 for the rest of the trip, so on its own it calls a moving truck
switched off.

State lives at %USERPROFILE%\\.grain-map-secrets\\trails.json, outside this
public repo.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, iso, metres, parse_iso, read_json, write_private  # noqa: E402

_parse, _iso = parse_iso, iso


def _metres(a: dict, b: dict) -> float:
    return metres(a["y"], a["x"], b["y"], b["x"])


def _load() -> dict:
    data = read_json(STATE, {})
    data.setdefault("vehicles", {})
    data.setdefault("events", [])
    return data


def _save(data: dict) -> None:
    write_private(STATE, data, separators=(",", ":"))


STATE = SECRETS / "trails.json"

# How much of the path to keep and show.
TRAIL_HOURS = 24

# How far a parked truck's GPS wanders. Measured across every quiet stretch
# in a day of breadcrumbs: the ones where the truck stayed put came back 8 to
# 18 m away, the nearest one that had actually driven was 73 m. Anywhere in
# that gap works; 60 m sits in it with room on both sides.
#
# Breadcrumb speed is deliberately not part of this. It is kept for drawing
# the trail, but on this hardware it disagrees with the ground: two of the
# three real stops in a day of data carry 2.4 km/h on the last point before
# the truck sat still for twenty minutes and travelled eight metres.
# Displacement is the measurement; reported speed is a derived number.
SAME_SPOT_M = 60.0

# Report a stop at least this long. The farmer asked for ten minutes.
IDLE_MIN = 10.0

# An alternator holds the system above this; a battery sitting on its own
# falls below it. Measured on these trucks: engine running reads 13.2 to
# 14.5 V, engine off reads 12.3 to 12.9 V, and cranking dips to 11.8 V. The
# threshold sits in a gap with a quarter-volt of clearance on either side.
#
# This matters more than it looks. Without it every stop reads as idling,
# and all three stops on the day this was written were a truck parked with
# the key out - so every alert would have been wrong.
ENGINE_ON_VOLTS = 13.1

# How old a voltage reading may be and still mean "running right now". The
# tracker heartbeats about hourly while awake, and a shutdown always sends
# its own reports (terminalPowerState stepping 0 -> 1 -> 2 as it drops to
# battery), so a reading that showed the alternator stays true until either
# a shutdown report or a missed heartbeat. An hour plus slack covers both.
# This was 20 minutes, which was shorter than the 30-minute gap between
# re-reads of a standing truck - so an idling truck could hold the state
# for at most two pushes in three, and usually none.
ENGINE_FRESH_MIN = 65.0

# Breadcrumbs arrive every 30-40 s while a truck is rolling, so a speed older
# than this is not what the truck is doing now. Tighter than the voltage
# window because speed changes by the second and a stale one reads as a lie.
SPEED_FRESH_MIN = 10.0

# Keep stops for a week, and never let the file grow without bound.
EVENT_DAYS = 7
MAX_EVENTS = 400

# Only ask for breadcrumbs from trucks that have reported recently. A truck
# parked for a fortnight has none to give, and asking 37 times every five
# minutes is a lot of requests for nothing.
FETCH_IF_REPORTED_WITHIN_H = TRAIL_HOURS

# A truck that is moving is worth re-reading every push; one standing still
# with the engine off is not, and this is what keeps the request count sane
# - a few vehicles per push instead of twenty. A standing truck whose engine
# was last seen RUNNING is re-read every push too: that is the one state
# the map exists to show, and it can end any minute.
STOPPED_REFRESH_MIN = 30.0


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


def read_engine_rows(values: list[dict]) -> list[dict]:
    """Device state reports -> {t, volts, on}, oldest first.

    `on` is decided by voltage because that is the physical measurement.
    engineState corroborates it but cannot stand alone: Deere sets it to 1
    on a start and leaves it 0 for the whole of a running trip, so trusting
    it would call every moving truck switched off.
    """
    rows = []
    for r in values or []:
        when, volts = r.get("time"), r.get("batteryVoltage")
        if not when or volts is None or _parse(when) is None:
            continue                # a time that cannot be read is no evidence
        rows.append({"t": when, "volts": round(float(volts), 2),
                     "on": float(volts) >= ENGINE_ON_VOLTS
                           or r.get("engineState") == 1})
    rows.sort(key=lambda r: r["t"])
    return rows


def fetch_engine(api, token: str, principal_id, limit: int = 200) -> list[dict]:
    """Engine state over time for one machine."""
    url = (f"https://api.deere.com/platform/machines/{principal_id}"
           f"/deviceStateReports?itemLimit={limit}")
    status, body = api(token, url)
    if status != 200 or not isinstance(body, dict):
        return []
    return read_engine_rows(body.get("values") or [])


def engine_runs(reports: list[dict]) -> list[tuple[str, str]]:
    """Periods the engine was running, as (start, end) timestamps.

    A run opens on the first report showing power and closes on the first
    showing none. The closing report is the shutdown itself - voltage has
    already fallen by the time it is sent - so the run ends there rather
    than at the last report that still showed the alternator.
    """
    runs, start = [], None
    for r in reports:
        if r["on"] and start is None:
            start = r["t"]
        elif not r["on"] and start is not None:
            runs.append((start, r["t"]))
            start = None
    if start is not None:
        runs.append((start, reports[-1]["t"]))
    return runs


def _overlap_minutes(a_start, a_end, b_start, b_end) -> float:
    lo, hi = max(a_start, b_start), min(a_end, b_end)
    return max(0.0, (hi - lo).total_seconds() / 60)


def classify_stops(stops: list[dict], reports: list[dict]) -> list[dict]:
    """Split each stop into engine-running and engine-off time.

    Adds `idle_min` - stationary with the engine running, which is idling in
    the sense Operations Center means and the farmer asked for - and `engine`,
    one of "idling", "parked" or "unknown".

    The engine record is a set of intervals, so a stop is decidable wherever
    the record reaches: the state cannot change between two reports without
    a report, and a shutdown always sends one. Only a stop the record does
    not reach into at all - it ended before the oldest report on hand - is
    "unknown", with idle_min None, because a stop that cannot be classified
    must not be reported as idling. A stop the record joins partway through
    gets its engine minutes counted from there, a lower bound, which errs
    toward "parked" and so toward not texting.

    (An earlier version wanted a report to land strictly INSIDE the stop,
    which called a 14-minute stop bracketed by two alternator readings
    unknown and dropped the exact alert this exists to send. The version
    after that wanted the record to begin before the stop, which threw out
    a real 21-minute stop whose first report came 30 seconds in.)
    """
    for stop in stops:
        stop["idle_min"], stop["engine"] = None, "unknown"
    if not reports:
        return stops
    oldest = _parse(reports[0]["t"])
    runs = [(_parse(a), _parse(b)) for a, b in engine_runs(reports)]
    runs = [(a, b) for a, b in runs if a and b]
    still_running = bool(reports[-1]["on"])
    for stop in stops:
        a, b = _parse(stop["start"]), _parse(stop["end"])
        if not a or not b or oldest is None or b < oldest:
            continue
        idle = 0.0
        for i, (ra, rb) in enumerate(runs):
            # The newest run has no shutdown report yet, so it is still going
            # - including through the rest of a stop that outlasts the last
            # heartbeat.
            if still_running and i == len(runs) - 1:
                rb = max(rb, b)
            idle += _overlap_minutes(a, b, ra, rb)
        stop["idle_min"] = round(idle)
        stop["engine"] = "idling" if idle >= IDLE_MIN else "parked"
    return stops


def find_stops(points: list[dict]) -> list[dict]:
    """Stretches of at least IDLE_MIN where the truck did not move.

    A quiet stretch only ends the stop if the truck is somewhere else when
    the tracker wakes up. If it is in the same place, it sat there the whole
    time and the silence counts towards the stop - which is what makes a
    parked truck reportable at all, since a parked truck stops reporting.

    "Somewhere else" is measured from where the stop began, not from the
    previous breadcrumb. Against the previous breadcrumb, a semi creeping up
    a scale line at 40 m a crumb never moved "far enough" and a kilometre of
    queue became one long idle. Against the anchor, it leaves the spot on
    the second crumb. A real stop's GPS wanders 8-18 m over hours, so the
    anchor holds.
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
        if _metres(run[0], p) > SAME_SPOT_M:
            close(run)
            run = [p]
        else:
            run.append(p)
    close(run)
    return stops


def remember(events: list[dict], key: str, truck: dict, stop: dict) -> tuple[dict, bool]:
    """Fold a stop into the log. Returns (event, is_new).

    The same truck, at the same spot, over an overlapping time is the same
    stop - still going, or seen again after the 24-hour trail window slid
    past its first breadcrumb and gave it a later start. Either way the
    existing entry is brought up to date rather than a second one written.
    Keyed on the start timestamp, as this once was, a truck parked all night
    read "10 min" until morning, and a stop that outlived the window was
    logged and texted twice.

    A stop that has only now been found idling is marked with a private
    `_now_idling` flag so the caller can text it once; the flag is not
    saved.
    """
    a, b = _parse(stop["start"]), _parse(stop["end"])
    matches = []
    for e in events:
        if e.get("id") != key:
            continue
        ea, eb = _parse(e.get("start")), _parse(e.get("end"))
        if not (a and b and ea and eb) or a > eb or b < ea:
            continue
        if _metres(e, stop) > SAME_SPOT_M:
            continue
        matches.append(e)
    if not matches:
        event = {"id": key, "name": truck.get("name"), "kind": truck.get("kind"),
                 **stop}
        events.append(event)
        return event, True

    # Every match is the same stop. More than one means the start-keyed
    # version of this wrote duplicates when the window slid; they are
    # folded into the earliest and dropped, so the log heals itself.
    matches.sort(key=lambda e: e.get("start") or "")
    e, dups = matches[0], matches[1:]
    for dup in dups:
        events.remove(dup)
    was_idling = any(m.get("engine") == "idling" for m in matches)
    ea = _parse(e["start"])
    # The earlier start is the truth - the trail window can only cut a
    # start short, never invent an earlier one. The NEW end is the truth -
    # the trail always holds the latest crumbs, so it knows whether the
    # stop is still going or when it ended, and a stored end from an older,
    # looser measurement must not outlive it.
    start, end = min(a, ea), b
    e.update({"start": stop["start"] if a <= ea else e["start"],
              "end": stop["end"],
              "minutes": round((end - start).total_seconds() / 60),
              "y": stop["y"], "x": stop["x"],
              "engine": stop.get("engine", "unknown"),
              "idle_min": stop.get("idle_min")})
    e["_now_idling"] = e["engine"] == "idling" and not was_idling
    return e, False


def prune(events: list[dict], now: _dt.datetime) -> list[dict]:
    """Newest first, a week deep, never more than MAX_EVENTS.

    The cap is applied AFTER sorting newest-first, so it is the oldest that
    fall off. With the slice before the sort, the file held its oldest week
    and threw away every new stop once it was full."""
    horizon = now - _dt.timedelta(days=EVENT_DAYS)
    kept = [e for e in events if (_parse(e.get("end")) or now) >= horizon]
    kept.sort(key=lambda e: e.get("end") or "", reverse=True)
    return kept[:MAX_EVENTS]


def update(api, token: str, trucks: list[dict], index: dict,
           budget: int = 20) -> tuple[int, list[dict], list[dict]]:
    """Refresh trails and long stops. Returns (trucks fetched, new stops,
    stops in the last 24 h) - the last so the caller need not re-read the
    file it just wrote.

    Each truck is asked only for breadcrumbs newer than the last one already
    held, so a busy truck costs one small request per push and a parked one
    costs nothing at all.
    """
    data = _load()
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(hours=TRAIL_HOURS)
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
        if (not truck.get("moving") and not record.get("engine_on")
                and checked is not None
                and (now - checked).total_seconds() / 60 < STOPPED_REFRESH_MIN):
            continue                       # standing still, engine off; nothing has changed
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

        # A device-state read does double duty: it classifies the stops, and
        # its newest row is whether the engine is running right now - which
        # is what tells "idling" from "stopped" on the map. A moving truck
        # with no stop on its trail needs neither, so it is not asked.
        stops = find_stops(points)
        reports = []
        if stops or not truck.get("moving"):
            reports = fetch_engine(api, token, machine["principal_id"])
        if reports:
            record["engine_on"] = bool(reports[-1]["on"])
            record["engine_at"] = reports[-1]["t"]
            record["volts"] = reports[-1]["volts"]

        # Every stop on the trail, every time: an ongoing one keeps growing
        # and can turn from parked to idling. `remember` decides which are
        # new, and which have only now become worth a text.
        for stop in classify_stops(stops, reports):
            event, is_new = remember(data["events"], key, truck, stop)
            if is_new or event.pop("_now_idling", False):
                new_stops.append(event)
            event.pop("_now_idling", None)

    data["events"] = prune(data["events"], now)
    _save(data)

    for truck in trucks:
        record = data["vehicles"].get(_key(truck))
        truck["trail"] = [[p["y"], p["x"]] for p in record["points"]] if record else []
        # Owned here and nowhere else. jd_idle used to write this from an
        # operating-hours field that is dead on these trackers.
        truck["engine_on"] = False
        if record and record.get("points"):
            # Speed off the newest breadcrumb. Deere reports it as km1hr-1 -
            # the unit is on the field and was checked against ground covered
            # between points, which agreed. It is only carried while it is
            # fresh, for the same reason the voltage is: a breadcrumb from an
            # hour ago says how fast the truck WAS going.
            newest = record["points"][-1]
            at = _parse(newest["t"])
            if at is not None and (now - at).total_seconds() / 60 <= SPEED_FRESH_MIN:
                truck["speed_kmh"] = newest["s"]
                truck["speed_at"] = newest["t"]
        if record and record.get("engine_at"):
            # Voltage read while the truck was last awake. It goes stale the
            # same way a position does, so it is only trusted as "running
            # now" while it is recent - otherwise the truck is stopped, which
            # is what a tracker that has gone back to sleep means anyway.
            seen_at = _parse(record["engine_at"])
            fresh_enough = (seen_at is not None
                            and (now - seen_at).total_seconds() / 60
                            <= ENGINE_FRESH_MIN)
            truck["engine_on"] = bool(record.get("engine_on")) and fresh_enough
            truck["volts"] = record.get("volts")
            truck["engine_at"] = record["engine_at"]
    return fetched, new_stops, recent_events(24, data["events"])


def recent_events(hours: int = 24, events: list[dict] | None = None) -> list[dict]:
    """Stops that ended within the window; from `events` if given, else from
    the file."""
    if events is None:
        events = _load()["events"]
    horizon = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
    return [e for e in events if (_parse(e.get("end")) or horizon) >= horizon]
