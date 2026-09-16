"""The camera watcher's logic, without a camera or a detector."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "dev"))

import cam_watch  # noqa: E402

CAM = {"id": "t", "min_box_height": 40,
       "line": [[0, 300], [700, 300], [700, 570], [0, 570]],
       "pits": {"pit1": [[240, 190], [350, 190], [350, 285], [240, 285]]}}


def box(cls, x1, y1, x2, y2, conf=0.9):
    return {"cls": cls, "conf": conf, "x1": x1, "y1": y1, "x2": x2, "y2": y2}


def test_point_in_polygon_square_and_edge_cases():
    sq = [[0, 0], [10, 0], [10, 10], [0, 10]]
    assert cam_watch.point_in_polygon(5, 5, sq)
    assert not cam_watch.point_in_polygon(15, 5, sq)
    assert not cam_watch.point_in_polygon(5, -1, sq)


def test_trucks_are_placed_by_their_wheels():
    """A tall truck whose box top is above the line region but whose wheels
    are on the apron is in line; one parked under the pit is at the pit."""
    counted = cam_watch.count_regions([
        box("truck", 300, 250, 400, 420),     # feet at y=420: on the apron
        box("truck", 270, 200, 330, 270),     # feet at y=270 inside pit1
    ], CAM)
    assert counted["line"] == 1
    assert counted["pits"] == {"pit1": True}


def test_small_boxes_and_low_confidence_do_not_count():
    counted = cam_watch.count_regions([
        box("truck", 300, 400, 340, 430),           # 30 px tall: a pickup far off
        box("truck", 300, 350, 400, 500, conf=0.2),  # not sure it is anything
        box("train", 400, 200, 600, 260),            # rail cars
    ], CAM)
    assert counted["line"] == 0 and counted["pits"] == {"pit1": False}


def test_pit_cycles_count_refills_only():
    """busy, open, busy, busy, open, busy = two trucks through."""
    t0 = "2026-09-16T17:%02d:00Z"
    hist = [{"t": t0 % (i * 2), "pits": {"pit1": b}} for i, b in
            enumerate([True, False, True, True, False, True])]
    import datetime as dt
    since = dt.datetime(2026, 9, 16, 16, 0, tzinfo=dt.timezone.utc)
    served, span = cam_watch.pit_cycles(hist, since)
    assert served == 2 and span == 10


def test_pace_prefers_camera_then_own_trucks_then_default():
    import datetime as dt
    now = dt.datetime(2026, 9, 16, 18, 0, tzinfo=dt.timezone.utc)
    cam = {"default_minutes_per_truck": 5}
    hist = [{"t": f"2026-09-16T17:{m:02d}:00Z", "pits": {"pit1": b}} for m, b in
            [(0, True), (4, False), (8, True), (12, False), (16, True), (20, False), (24, True)]]
    per, src = cam_watch.pace(cam, hist, [], now)
    assert abs(per - 24 / 3) < 1e-9 and "pit turned over 3" in src
    visits = [{"vin": "v", "start": "2026-09-15T10:00:00Z", "end": "2026-09-15T10:34:00Z", "minutes": 34.0}]
    per, src = cam_watch.pace(cam, [], visits, now)
    assert per == 34.0 and "own trucks" in src
    per, src = cam_watch.pace(cam, [], [], now)
    assert per == 5 and "default" in src


def _state_with(readings):
    return {"cameras": {"t": readings}, "errors": []}


def test_summary_lists_an_unread_camera_and_its_last_error():
    import datetime as dt
    now = dt.datetime(2026, 9, 16, 18, 0, tzinfo=dt.timezone.utc)
    cam = dict(CAM, name="Test", url="https://x/y", hours=[0, 24])
    state = {"cameras": {}, "errors": [{"t": "2026-09-16T17:58:00Z", "camera": "t", "error": "no frame"}]}
    out = cam_watch.summary([cam], state, now, trails={})
    assert out["generated_at"] == "2026-09-16T18:00:00Z"
    (entry,) = out["cameras"]
    assert entry["at"] is None and entry["error"] == "no frame" and entry["open"]
    assert entry["name"] == "Test" and entry["url"] == "https://x/y"


def test_summary_carries_wait_pace_and_todays_line_and_nothing_heavy():
    import datetime as dt
    now = dt.datetime(2026, 9, 16, 18, 0, tzinfo=dt.timezone.utc)
    cam = dict(CAM, default_minutes_per_truck=4, hours=[0, 24])
    hist = [{"t": f"2026-09-16T17:{m:02d}:00Z", "line": n, "pits": {"pit1": False}, "vehicles": n}
            for m, n in [(0, 0), (2, 1), (4, 3), (30, 2)]]
    out = cam_watch.summary([cam], _state_with(hist), now, trails={})
    (entry,) = out["cameras"]
    assert entry["line"] == 2 and entry["per_truck"] == 4.0 and entry["wait_min"] == 8
    assert "default" in entry["source"] and "error" not in entry
    # thinned to ten-minute steps, keeping the busiest reading in each
    assert [p["line"] for p in entry["today"]] == [3, 2]
    assert "boxes" not in json_dump(entry) and "line\": [[" not in json_dump(entry)


def json_dump(obj):
    import json
    return json.dumps(obj)


def test_push_skips_when_nothing_changed_and_pushes_again_after_ten_minutes(monkeypatch, tmp_path):
    import json
    relay = tmp_path / "relay.json"
    relay.write_text(json.dumps({"url": "https://relay.test/", "push_token": "p"}))
    monkeypatch.setattr(cam_watch, "RELAY", relay)
    sent = []

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok":true}'

    def fake_open(req, timeout):
        sent.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
        return _Resp()
    monkeypatch.setattr(cam_watch.urllib.request, "urlopen", fake_open)
    cam = dict(CAM, hours=[0, 24])
    hist = [{"t": "2026-09-16T17:00:00Z", "line": 1, "pits": {"pit1": True}}]
    state = _state_with(hist)

    assert cam_watch.push([cam], state) == '{"ok":true}'
    assert sent[0][0] == "https://relay.test/cameras" and sent[0][1] == "Bearer p"
    assert sent[0][2]["cameras"][0]["line"] == 1
    assert cam_watch.push([cam], state) == "unchanged" and len(sent) == 1
    # the count changes: pushed at once
    hist.append({"t": "2026-09-16T17:02:00Z", "line": 2, "pits": {"pit1": True}})
    assert cam_watch.push([cam], state) == '{"ok":true}' and len(sent) == 2
    # nothing changes but the last push is old: pushed so "as of" moves
    state["relay"]["at"] = "2026-09-16T17:00:00Z"
    assert cam_watch.push([cam], state) == '{"ok":true}' and len(sent) == 3


def test_push_is_a_no_op_without_a_relay(monkeypatch, tmp_path):
    monkeypatch.setattr(cam_watch, "RELAY", tmp_path / "missing.json")
    assert cam_watch.push([CAM], _state_with([])) is None


def test_block_is_ascii_and_carries_only_the_read_token():
    import cam_build_block
    html = cam_build_block.build("https://relay.test", "READ123")
    assert all(ord(c) < 128 for c in html)
    assert '"READ123"' in html and "__RELAY_" not in html
    assert "/cameras" in html and "Authorization" in html


def test_own_visits_split_on_a_long_gap_and_ignore_far_points():
    pin = (42.3295, -93.6649)
    pts = []
    def p(m, dlat=0.0):
        return {"t": f"2026-09-16T1{m // 60}:{m % 60:02d}:00Z", "y": pin[0] + dlat, "x": pin[1], "s": 0}
    pts += [p(0), p(5), p(10), p(30)]          # a 30-minute visit
    pts += [p(31, 0.05)]                        # 5 km away: not there
    pts += [p(90), p(100)]                      # back after an hour: a second, 10-minute visit
    trails = {"vehicles": {"vin1": {"points": pts}}}
    visits = cam_watch.own_visits(pin[0], pin[1], trails)
    assert [v["minutes"] for v in visits] == [30.0, 10.0]
