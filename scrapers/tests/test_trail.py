"""Stop detection, against the cases that actually occurred.

Every fixture here is taken from real breadcrumbs off the farm's own trucks
on 2026-09-12, because the bug this guards was not a coding mistake - the
code did exactly what it was written to do - but a wrong belief about what
the hardware reports. Invented fixtures would have agreed with the wrong
belief and passed.

Two beliefs turned out to be false, and each one alone was enough to make
the alerts fire never:

  * that a quiet tracker means the truck is unaccounted for. It means the
    truck parked; the silence is the stop.
  * that the reported speed says whether the truck is moving. Two of the
    three real stops carry 2.4 km/h on the last breadcrumb before the truck
    sat still for twenty minutes and moved eight metres.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "dev"))

import jd_trail  # noqa: E402


def pt(t, y, x, s=0.0):
    return {"t": f"2026-09-12T{t}.000Z", "y": y, "x": x, "s": s}


def test_sleeping_tracker_is_a_stop_not_a_gap():
    """The 435-minute park. One breadcrumb, seven hours of silence, then one
    more from the same spot: the truck sat there the whole time."""
    stops = jd_trail.find_stops([
        pt("12:31:44", 43.08166, -93.82323, 0.9),
        pt("19:46:00", 43.08167, -93.82322, 0.0),   # 9 m away, 434 min later
    ])
    assert len(stops) == 1
    assert stops[0]["minutes"] == 434


def test_reported_speed_does_not_veto_a_stop():
    """The 21-minute stop. Speed says 2.4 km/h, the ground says eight metres
    in twenty minutes. The ground wins."""
    stops = jd_trail.find_stops([
        pt("18:48:36", 43.10000, -93.80000, 2.4),
        pt("19:08:09", 43.10005, -93.80003, 0.0),
    ])
    assert len(stops) == 1
    assert stops[0]["minutes"] >= 19


def test_driving_away_during_the_quiet_is_not_a_stop():
    """The 368-minute gap where the truck turned up 2 km away. It went
    somewhere unobserved; that is not a truck standing still."""
    assert jd_trail.find_stops([
        pt("12:28:47", 43.10000, -93.80000, 8.1),
        pt("18:36:25", 43.11800, -93.80500, 0.0),   # 2,041 m away
    ]) == []


def test_short_stop_is_below_the_threshold():
    assert jd_trail.find_stops([
        pt("12:00:00", 43.10000, -93.80000),
        pt("12:08:00", 43.10000, -93.80000),
    ]) == []


def test_stop_is_reported_at_where_it_stopped():
    stops = jd_trail.find_stops([
        pt("12:00:00", 43.10000, -93.80000),
        pt("12:30:00", 43.10002, -93.80001),
    ])
    assert (stops[0]["y"], stops[0]["x"]) == (43.10002, -93.80001)


def test_two_stops_separated_by_a_drive():
    points = [
        pt("08:00:00", 43.10000, -93.80000),
        pt("08:20:00", 43.10000, -93.80000),        # 20 min here
        pt("08:30:00", 43.20000, -93.90000),        # drove 13 km
        pt("09:00:00", 43.20000, -93.90000),        # 30 min there
    ]
    stops = jd_trail.find_stops(points)
    assert [s["minutes"] for s in stops] == [20, 30]


def test_moving_truck_produces_nothing():
    """Forty-second breadcrumbs down a road at highway speed."""
    points = [pt(f"08:{m:02d}:00", 43.10000 + 0.006 * m, -93.80000, 55.0)
              for m in range(0, 40)]
    assert jd_trail.find_stops(points) == []


def test_creeping_truck_is_not_a_stop():
    """A semi inching up a scale line: 40 m every 35 s for 15 minutes. Each
    hop is under the jitter radius, but it ends up 1 km from where it
    started - measured against the anchor, it left the spot at once."""
    points = []
    for i in range(26):
        s = i * 35
        points.append({"t": f"2026-09-12T09:{s // 60:02d}:{s % 60:02d}.000Z",
                       "y": 43.10000 + i * 0.00036, "x": -93.80000, "s": 4.0})
    assert jd_trail.find_stops(points) == []


def test_parked_truck_with_gps_wander_is_one_stop():
    """Twenty crumbs over an hour, each a few metres off the last."""
    points = [{"t": f"2026-09-12T09:{3 * i:02d}:00.000Z",
               "y": 43.10000 + (i % 3) * 0.00005, "x": -93.80000 + (i % 2) * 0.00008,
               "s": 0.0} for i in range(20)]
    stops = jd_trail.find_stops(points)
    assert len(stops) == 1 and stops[0]["minutes"] == 57


# --- engine state -------------------------------------------------------
#
# Every voltage below is a real reading off these trucks on 2026-09-12.

def rep(t, volts, engine_state=0):
    """A device state report in the shape Deere actually sends."""
    return {"time": f"2026-09-12T{t}.000Z", "batteryVoltage": volts,
            "engineState": engine_state}


def reps(*rows):
    return jd_trail.read_engine_rows(list(rows))


def test_threshold_sits_in_the_gap_between_the_two_populations():
    """Engine off read 12.32-12.97 V across the fleet that day; engine on
    read 13.20-14.53. The threshold has to fall between, with room."""
    assert 12.97 < jd_trail.ENGINE_ON_VOLTS < 13.20


def test_engine_runs_pair_up_starts_and_shutdowns():
    runs = jd_trail.engine_runs(reps(
        rep("12:10:50", 14.48),               # alternator
        rep("12:11:17", 12.32),               # shutting down
        rep("12:11:50", 12.53),
        rep("12:23:27", 13.90, 1),            # restarted
        rep("12:40:00", 12.60),
    ))
    assert runs == [("2026-09-12T12:10:50.000Z", "2026-09-12T12:11:17.000Z"),
                    ("2026-09-12T12:23:27.000Z", "2026-09-12T12:40:00.000Z")]


def test_parked_stop_is_not_idling():
    """Red Impala East, 18:48. Sat 21 minutes, engine ran for one of them."""
    stops = [{"start": "2026-09-12T18:48:15.000Z",
              "end": "2026-09-12T19:09:12.000Z", "minutes": 21}]
    jd_trail.classify_stops(stops, reps(
        rep("18:48:45", 14.53),
        rep("18:49:11", 12.68),
        rep("18:49:44", 12.59),
        rep("19:04:13", 12.79),
        rep("19:08:00", 12.78),
    ))
    assert stops[0]["engine"] == "parked"
    assert stops[0]["idle_min"] < jd_trail.IDLE_MIN


def test_engine_running_throughout_is_idling():
    stops = [{"start": "2026-09-12T14:00:00.000Z",
              "end": "2026-09-12T14:30:00.000Z", "minutes": 30}]
    jd_trail.classify_stops(stops, reps(
        rep("13:59:00", 14.10),
        rep("14:15:00", 14.20),
        rep("14:31:00", 12.60),
    ))
    assert stops[0]["engine"] == "idling"
    assert stops[0]["idle_min"] == 30


def test_stop_that_began_before_the_record_is_unknown_not_idling():
    """The one that must never be guessed: no evidence is not evidence."""
    stops = [{"start": "2026-09-12T14:00:00.000Z",
              "end": "2026-09-12T14:30:00.000Z", "minutes": 30}]
    jd_trail.classify_stops(stops, reps(rep("15:00:00", 14.10)))
    assert stops[0]["engine"] == "unknown"
    assert stops[0]["idle_min"] is None
    jd_trail.classify_stops(stops, [])
    assert stops[0]["engine"] == "unknown"


def test_stop_bracketed_by_alternator_readings_is_idling():
    """No report lands inside the 14-minute window, but the engine was on
    at 09:57 and still on at 10:19: the state cannot have changed without a
    shutdown report. This is the exact alert that was being dropped."""
    stops = [{"start": "2026-09-12T10:00:00.000Z",
              "end": "2026-09-12T10:14:00.000Z", "minutes": 14}]
    jd_trail.classify_stops(stops, reps(
        rep("09:57:00", 14.10), rep("10:19:00", 14.10), rep("10:40:00", 12.60)))
    assert stops[0]["engine"] == "idling"
    assert stops[0]["idle_min"] == 14


def test_open_engine_run_covers_a_stop_past_the_last_heartbeat():
    """Last report showed the alternator at 10:05; the stop runs to 10:30
    with no shutdown report. No shutdown report means still running."""
    stops = [{"start": "2026-09-12T10:00:00.000Z",
              "end": "2026-09-12T10:30:00.000Z", "minutes": 30}]
    jd_trail.classify_stops(stops, reps(rep("09:50:00", 14.20), rep("10:05:00", 14.10)))
    assert stops[0]["engine"] == "idling"
    assert stops[0]["idle_min"] == 30


def test_unreadable_report_time_is_dropped_not_crashed():
    rows = jd_trail.read_engine_rows([
        {"time": "2026-09-12 10:00:00 CDT", "batteryVoltage": 14.1},
        rep("10:01:00", 14.1)])
    assert [r["t"] for r in rows] == ["2026-09-12T10:01:00.000Z"]


# --- the stop log ---------------------------------------------------------

def _now():
    import datetime as dt
    return dt.datetime(2026, 9, 12, 20, 0, tzinfo=dt.timezone.utc)


def test_prune_keeps_the_newest_stops_not_the_oldest():
    events = [{"id": "t", "start": f"2026-09-{d:02d}T10:00:00Z",
               "end": f"2026-09-{d:02d}T10:30:00Z"} for d in range(6, 13)]
    events = events * 80                        # 560 entries, over the cap
    kept = jd_trail.prune(events, _now())
    assert len(kept) == jd_trail.MAX_EVENTS
    assert kept[0]["end"] == "2026-09-12T10:30:00Z"      # newest first
    assert all(e["end"] >= "2026-09-07T10:30:00Z" for e in kept)


def test_prune_drops_stops_older_than_a_week():
    old = {"id": "t", "start": "2026-09-01T10:00:00Z", "end": "2026-09-01T10:30:00Z"}
    new = {"id": "t", "start": "2026-09-12T10:00:00Z", "end": "2026-09-12T10:30:00Z"}
    assert jd_trail.prune([old, new], _now()) == [new]


def _stop(start, end, y=43.1, x=-93.8, **more):
    m = {"start": f"2026-09-12T{start}:00.000Z", "end": f"2026-09-12T{end}:00.000Z",
         "y": y, "x": x, "engine": "parked", "idle_min": 0}
    m.update(more)
    import datetime as dt
    a = dt.datetime.fromisoformat(m["start"].replace("Z", "+00:00"))
    b = dt.datetime.fromisoformat(m["end"].replace("Z", "+00:00"))
    m["minutes"] = round((b - a).total_seconds() / 60)
    return m


def test_ongoing_stop_grows_instead_of_freezing():
    events = []
    truck = {"name": "Red 1Ton", "kind": "pickup"}
    e, new = jd_trail.remember(events, "vin1", truck, _stop("18:00", "18:10"))
    assert new and e["minutes"] == 10
    e2, new = jd_trail.remember(events, "vin1", truck, _stop("18:00", "23:30"))
    assert not new and e2 is e and len(events) == 1
    assert e["minutes"] == 330


def test_stop_seen_again_with_a_later_start_is_the_same_stop():
    """The 24-hour trail window slid past the first breadcrumb."""
    events = []
    truck = {"name": "Red 1Ton", "kind": "pickup"}
    jd_trail.remember(events, "vin1", truck, _stop("18:00", "23:00"))
    _, new = jd_trail.remember(events, "vin1", truck, _stop("19:45", "23:30"))
    assert not new and len(events) == 1
    assert events[0]["start"] == "2026-09-12T18:00:00.000Z"
    assert events[0]["end"] == "2026-09-12T23:30:00.000Z"


def test_same_time_different_spot_or_truck_is_a_different_stop():
    events = []
    truck = {"name": "Red 1Ton", "kind": "pickup"}
    jd_trail.remember(events, "vin1", truck, _stop("18:00", "18:30"))
    jd_trail.remember(events, "vin1", truck, _stop("18:00", "18:30", y=43.2))
    jd_trail.remember(events, "vin2", truck, _stop("18:00", "18:30"))
    assert len(events) == 3


def test_stop_that_turns_idling_is_flagged_once():
    events = []
    truck = {"name": "Red 1Ton", "kind": "pickup"}
    e, _ = jd_trail.remember(events, "vin1", truck, _stop("18:00", "18:12"))
    assert not e.get("_now_idling")
    e, _ = jd_trail.remember(events, "vin1", truck,
                             _stop("18:00", "18:25", engine="idling", idle_min=13))
    assert e["_now_idling"]
    e, _ = jd_trail.remember(events, "vin1", truck,
                             _stop("18:00", "18:40", engine="idling", idle_min=28))
    assert not e["_now_idling"]
