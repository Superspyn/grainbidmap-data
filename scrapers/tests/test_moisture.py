"""The overnight moisture calculator: the model the page ships, run under
Node exactly as the browser runs it, and the farm PC feed that fills it in.

No field names, operator names or real coordinates appear here - this repo
is public."""
import datetime as dt
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "dev"))

import moisture_build_block as mb  # noqa: E402
import moisture_feed as mf  # noqa: E402


# ---------------------------------------------------------------------------
# the model, under Node

NODE_CASES = r"""
var G = require(process.argv[2]);
var out = {};
var H = 3600000, T0 = Date.UTC(2026, 9, 1, 0, 0);   // 7 pm Central, Sep 30

// One fixture hour. dpd is the dew point depression in C.
function hr(i, o) {
  var t = o.t !== undefined ? o.t : 12;
  return { ms: T0 + i * H, hod: (19 + i) % 24, dayKey: 'd' + Math.floor((19 + i) / 24),
           day: o.day !== undefined ? o.day : false, t: t, td: t - (o.dpd !== undefined ? o.dpd : 6),
           rh: o.rh !== undefined ? o.rh : 70, wind: o.wind !== undefined ? o.wind : 1,
           sky: o.sky !== undefined ? o.sky : 10, qpf: o.qpf || 0, pop: 0 };
}

// The worked example from the extension tables: 60 F, 80% RH.
out.cornEmc = G.emc('corn', 15.556, 80);
out.soyEmc = G.emc('soybeans', 15.556, 80);
out.soyEmc90 = G.emc('soybeans', 15.556, 90);
out.cornEmc90 = G.emc('corn', 15.556, 90);
out.capped = [G.emc('soybeans', 10, 99), G.emc('soybeans', 10, 95)];

// Clear, calm: dew at a 2.8 C depression (the clear-night threshold is 3.0).
out.clearCalm = G.wetness([5, 4, 2.8, 2, 1.5, 3, 3.5, 4, 5].map(function (d, i) {
  return hr(i, { dpd: d, sky: 10, wind: 1 }); }));
// Cloudy and breezy: the same depressions, but dew waits for 2.0.
out.cloudyWindy = G.wetness([5, 4, 2.8, 2, 1.5, 3, 3.5, 4, 5].map(function (d, i) {
  return hr(i, { dpd: d, sky: 90, wind: 3, rh: 92 }); }));
out.rainHour = G.wetness([hr(0, { qpf: 1.0 }), hr(1, {})]);

out.durations = ['PT1H', 'PT6H', 'P1DT3H', 'PT30M', 'PT1H30M'].map(G.durationHours);
var spread = G.expand({ values: [{ validTime: '2026-10-01T00:00:00+00:00/PT6H', value: 6 }] }, true);
out.spread = Object.keys(spread).map(function (k) { return spread[k]; });
var held = G.expand({ values: [{ validTime: '2026-10-01T00:00:00+00:00/PT3H', value: 12 },
                               { validTime: '2026-10-01T03:00:00+00:00/PT1H', value: null }] }, false);
out.held = Object.keys(held).length;

// A dewy soybean night: 13 hours of night, dew for most of it, then a dry,
// breezy day with falling humidity.
function beanNight(opts) {
  var hs = [];
  for (var i = 0; i < 13; i++) hs.push(hr(i, { dpd: i < 3 ? 4 : 1.2, rh: 95, sky: 5, wind: 0.8, qpf: opts.rain && i === 6 ? opts.rain : 0 }));
  for (var j = 13; j < 25; j++) {
    var k = j - 13;
    hs.push(hr(j, { day: true, t: 12 + k, dpd: 2 + k * 1.5, rh: Math.max(35, 95 - k * 7), wind: 4, sky: 10 }));
  }
  for (var n = 25; n < 30; n++) hs.push(hr(n, { dpd: 6, rh: 70 }));
  return hs;
}
var dewy = G.analyse(beanNight({}), 'soybeans', 13.0, 13.0);
out.dewyLevel = dewy.level;
out.dewyBackAt = dewy.backAt;
out.dewyBackHod = dewy.backAt === null ? null : beanNight({})[dewy.backAt].hod;
var rained = G.analyse(beanNight({ rain: 4 }), 'soybeans', 13.0, 13.0);
out.rainLevel = rained.level;
out.rainBackAt = rained.backAt;

// A dry night: no dew anywhere.
var dryHours = [];
for (var q = 0; q < 13; q++) dryHours.push(hr(q, { dpd: 8, rh: 55, wind: 3 }));
for (var r = 13; r < 25; r++) dryHours.push(hr(r, { day: true, t: 18, dpd: 10, rh: 40, wind: 4 }));
out.dryLevel = G.analyse(dryHours, 'soybeans', 12.5, 13.0).level;

// Corn through the same dewy night barely moves, then dries by day.
var corn = G.analyse(beanNight({}), 'corn', 20.0, 19.0);
out.cornDawn = corn.dawn;
out.cornEnd = corn.track[corn.track.length - 1];
out.cornReach = corn.reachAt;

// A week of warm dry days takes 22% corn down roughly 0.4-0.8 points a day.
var week = [];
for (var w = 0; w < 24 * 7; w++) week.push(hr(w, { day: ((19 + w) % 24) >= 7 && ((19 + w) % 24) < 19,
  t: 18, dpd: 9, rh: 55, wind: 3 }));
var wk = G.analyse(week, 'corn', 22.0, 18.0);
out.cornWeekDrop = 22.0 - wk.track[wk.track.length - 1];

// Which reading fills "moisture now".
var now = Date.parse('2026-09-30T20:00:00Z');
var fields = [
  { id: 'a', c: 'corn', y: 43.00, x: -93.00 },
  { id: 'b', c: 'corn', y: 43.05, x: -93.00 },      // 5.6 km north
  { id: 'c', c: 'corn', y: 43.01, x: -93.00 },      // 1.1 km north
  { id: 'd', c: 'soybeans', y: 43.001, x: -93.00 },  // nearest, wrong crop
  { id: 'e', c: 'corn', y: 43.002, x: -93.00 }       // nearest corn, but stale
];
var readings = {
  b: { m: 21.0, end: '2026-09-30T15:00:00Z' },
  c: { m: 22.5, end: '2026-09-29T15:00:00Z' },
  d: { m: 14.0, end: '2026-09-30T15:00:00Z' },
  e: { m: 25.0, end: '2026-09-25T15:00:00Z' }
};
out.pickNearest = G.pickReading(fields[0], fields, readings, now);
readings.a = { m: 19.9, end: '2026-09-30T12:00:00Z' };
out.pickOwn = G.pickReading(fields[0], fields, readings, now);
out.pickNone = G.pickReading(fields[0], fields, {}, now);

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    d = tmp_path_factory.mktemp("gtm")
    (d / "model.js").write_text(mb.MODEL_JS, encoding="ascii")
    (d / "cases.js").write_text(NODE_CASES, encoding="ascii")
    r = subprocess.run([node, str(d / "cases.js"), str(d / "model.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_equilibrium_reproduces_the_extension_tables(model):
    """60 F and 80% RH: 16.1% corn (Chung-Pfost), 18.3% soybeans (Halsey)."""
    assert abs(model["cornEmc"] - 16.06) < 0.1
    assert abs(model["soyEmc"] - 18.25) < 0.1


def test_soybeans_climb_far_faster_than_corn_near_saturation(model):
    """80 -> 90% RH moves beans about 9.5 points and corn about 3."""
    assert model["soyEmc90"] - model["soyEmc"] > 7
    assert model["cornEmc90"] - model["cornEmc"] < 5


def test_humidity_above_the_fitted_range_is_capped(model):
    lo, hi = model["capped"]
    assert lo == hi


def test_dew_forms_earlier_on_a_clear_calm_night(model):
    """Pods radiate below air temperature under a clear sky, so dew starts at
    a 3.0 C depression there, 2.0 C otherwise; both dry off above 3.8 C."""
    # depressions 5, 4, 2.8, 2, 1.5, 3, 3.5, 4, 5 - wet from 2.8, dry again at 4.0
    assert model["clearCalm"] == [None, None, "dew", "dew", "dew", "dew", "dew", None, None]
    assert model["cloudyWindy"][2] is None and model["cloudyWindy"][3] == "dew"


def test_rain_wets_the_hour(model):
    assert model["rainHour"][0] == "rain"


def test_forecast_durations_and_rain_spread(model):
    # a part hour counts as a whole one, so a 90-minute span touches two
    assert model["durations"] == [1, 6, 27, 1, 2]
    assert model["spread"] == [1, 1, 1, 1, 1, 1]
    assert model["held"] == 3, "a null value is a gap, not a zero"


def test_a_dewy_night_rates_beans_high_and_says_when_they_are_back(model):
    assert model["dewyLevel"] == "high"
    assert model["dewyBackAt"] is not None
    assert 8 <= model["dewyBackHod"] <= 14, "back to 13% by late morning"


def test_rain_overnight_is_its_own_rating_and_slower_to_clear(model):
    assert model["rainLevel"] == "rain"
    assert model["rainBackAt"] is None or model["rainBackAt"] >= model["dewyBackAt"]


def test_a_dry_night_rates_low(model):
    assert model["dryLevel"] == "low"


def test_corn_barely_rewets_overnight(model):
    """Rewetting runs at a quarter of drying and the husk keeps dew off."""
    assert 0 <= model["cornDawn"] - 20.0 < 0.3


def test_corn_dries_at_the_measured_field_rate(model):
    """Iowa State and Purdue measured 0.4-0.8 points a day in fair weather."""
    per_day = model["cornWeekDrop"] / 7
    assert 0.4 <= per_day <= 1.0, per_day


def test_moisture_now_prefers_the_field_itself_then_the_nearest_same_crop(model):
    assert model["pickOwn"]["fieldId"] == "a" and model["pickOwn"]["km"] == 0
    near = model["pickNearest"]
    assert near["fieldId"] == "c", "nearest fresh corn reading, not the soybean or the stale one"
    assert abs(near["km"] - 1.11) < 0.05
    assert model["pickNone"] is None


def test_the_page_builds_pure_ascii_with_no_placeholder_left(tmp_path):
    html = mb.build("https://relay.example", "READTOKEN",
                    [{"id": "f1", "n": "Test Field 1", "c": "corn", "y": 43.0, "x": -93.0}])
    assert all(ord(c) < 128 for c in html)
    assert "__" not in html.replace("__proto__", "")
    assert '"READTOKEN"' in html and "/moisture" in html


def test_the_shipped_page_script_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    html = mb.build("https://relay.example", "T", [{"id": "f", "n": "F", "c": "soybeans", "y": 43, "x": -93}])
    script = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    p = tmp_path / "page.js"
    p.write_text(script, encoding="ascii")
    r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_field_list_carries_no_outline():
    fleet = {"fields": [{"id": "x", "name": "Test", "crop": "corn", "lat": 43.123456, "lon": -93.1,
                         "rings": [{"t": "e", "p": [[0, 0]]}]},
                        {"id": "y", "name": "No centroid", "crop": "corn", "lat": None, "lon": None}]}
    assert mb.field_list(fleet) == [{"id": "x", "n": "Test", "c": "corn", "y": 43.12346, "x": -93.1}]


# ---------------------------------------------------------------------------
# the farm PC feed

SQUARE = [{"t": "e", "p": [[43.0, -93.0], [43.0, -92.9], [43.1, -92.9], [43.1, -93.0]]},
          {"t": "i", "p": [[43.04, -92.96], [43.04, -92.94], [43.06, -92.94], [43.06, -92.96]]}]


def test_combines_are_known_by_model_not_name():
    feed = [
        {"kind": "equipment", "model": "X9 1100", "vin": "1TEST0000437", "lat": 43, "lon": -93, "at": "t"},
        {"kind": "equipment", "model": "S7 800", "vin": "X1234", "lat": 43, "lon": -93, "at": "t"},
        {"kind": "equipment", "model": "STS16", "vin": "Y9", "lat": 43, "lon": -93, "at": "t"},
        {"kind": "equipment", "model": "8R 410", "vin": "Z", "lat": 43, "lon": -93, "at": "t"},
        {"kind": "equipment", "model": "9RX 830", "vin": "Z", "lat": 43, "lon": -93, "at": "t"},
        {"kind": "equipment", "model": "616R", "vin": "Z", "lat": 43, "lon": -93, "at": "t"},
        {"kind": "semi", "model": "X9 lookalike", "vin": "Z", "lat": 43, "lon": -93, "at": "t"},
        {"kind": "equipment", "model": "X9 1000", "vin": "Q", "lat": None, "lon": None, "at": "t"},
    ]
    got = mf.combines(feed)
    assert [c["label"] for c in got] == ["X9 1100 #0437", "S7 800 #1234", "STS16 #Y9"]


def test_a_point_in_the_farmstead_hole_is_not_in_the_field():
    assert mf.point_in_rings(43.02, -92.98, SQUARE)
    assert not mf.point_in_rings(43.05, -92.95, SQUARE)
    assert not mf.point_in_rings(43.2, -92.95, SQUARE)


def test_field_at_uses_bounds_then_rings():
    fields = [{"id": "f1", "rings": SQUARE}]
    fields[0]["_bounds"] = mf.field_bounds(fields[0])
    assert mf.field_at(43.02, -92.98, fields) == "f1"
    assert mf.field_at(43.05, -92.95, fields) is None


def _t(minutes_ago, now):
    return mf.iso(now - dt.timedelta(minutes=minutes_ago))


def test_fields_to_read_catches_the_field_now_and_the_one_just_left():
    now = dt.datetime(2026, 9, 30, 20, 0, tzinfo=dt.timezone.utc)
    state = {"visits": {"now": _t(5, now), "just_read": _t(5, now),
                        "left": _t(60, now), "done": _t(300, now), "old": _t(60 * 24 * 9, now)},
             "read_at": {"just_read": _t(10, now), "left": _t(120, now), "done": _t(200, now)}}
    seen = {"now": _t(5, now), "just_read": _t(5, now)}
    got = set(mf.fields_to_read(state, seen, now))
    assert got == {"now", "left"}, got
    assert set(mf.fields_to_read(state, seen, now, ["a", "b"])) == {"a", "b"}


def test_payload_drops_old_readings():
    now = dt.datetime(2026, 9, 30, 20, 0, tzinfo=dt.timezone.utc)
    state = {"readings": {"new": {"m": 21, "end": _t(60, now)},
                          "old": {"m": 25, "end": _t(60 * 24 * 20, now)}}}
    body = mf.payload(state, [{"label": "X9 1100 #0001", "field": "new", "at": "t",
                               "lat": 43.123456, "lon": -93.1}], now)
    assert set(body["readings"]) == {"new"}
    assert body["combines"][0] == {"label": "X9 1100 #0001", "field": "new", "at": "t",
                                   "y": 43.12346, "x": -93.1}


def test_a_zero_moisture_pass_is_a_sensor_gap_not_dry_grain():
    """Two passes on this farm read 0.0 in September 2026."""
    calls = []

    def api(token, url):
        calls.append(url)
        if "fieldOperations" in url:
            return 200, {"values": [
                {"startDate": "2026-09-29T10:00:00Z", "endDate": "2026-09-29T20:00:00Z",
                 "cropName": "CORN_WET", "links": [{"rel": "harvestMoistureResult", "uri": "m-new"}]},
                {"startDate": "2026-09-20T10:00:00Z", "endDate": "2026-09-20T20:00:00Z",
                 "cropName": "CORN_WET", "links": [{"rel": "harvestMoistureResult", "uri": "m-old"}]}]}
        if url == "m-new":
            return 200, {"averageMoisture": {"value": 0.0}}
        return 200, {"averageMoisture": {"value": 21.44}}

    r = mf.harvest_reading(api, "tok", {"org": "1", "id": "f"}, 2026)
    assert r == {"m": 21.44, "crop": "corn", "start": "2026-09-20T10:00:00Z", "end": "2026-09-20T20:00:00Z"}
    assert "fieldOperationType=HARVEST&cropSeason=2026" in calls[0]
