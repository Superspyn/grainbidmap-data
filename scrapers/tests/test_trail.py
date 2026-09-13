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


def test_stop_with_no_device_reports_is_unknown_not_idling():
    """The one that must never be guessed: no evidence is not evidence."""
    stops = [{"start": "2026-09-12T14:00:00.000Z",
              "end": "2026-09-12T14:30:00.000Z", "minutes": 30}]
    jd_trail.classify_stops(stops, reps(rep("09:00:00", 14.10)))
    assert stops[0]["engine"] == "unknown"
    assert stops[0]["idle_min"] is None
