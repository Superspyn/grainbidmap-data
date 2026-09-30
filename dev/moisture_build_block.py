"""Build the overnight grain moisture calculator for a PASSWORD-PROTECTED page.

    python dev/moisture_build_block.py      # writes ~/.grain-map-secrets/moisture-block.html

Paste the file's contents into a Code block on the same private Squarespace
page as the map and the camera block. Pick a field, and it says whether the
standing crop is likely to pick up moisture tonight, how much, and when it
should be back to harvest moisture tomorrow.

Where each piece comes from:

  * the forecast - fetched live by the page from the National Weather
    Service for that field's own 1.5-mile grid square. The service sends
    Access-Control-Allow-Origin: *, so no farm PC sits in the middle and the
    forecast is as fresh as the moment the page is opened;
  * the fields, their crop and location - Operations Center, baked in here
    from ~/.grain-map-secrets/fleet.json;
  * what the combines are reading and where they are - Operations Center,
    pushed to the private relay's /moisture key every half hour by
    dev/moisture_feed.py.

Private because the field names and the relay's read token are inside.

The science, and why corn and soybeans are answered differently:

  Equilibrium moisture is ASAE D245.6's modified Chung-Pfost for corn
  (A 374.34, B 0.18662, C 31.696 - Chen and Morey 1989) and a modified
  Halsey for soybeans with coefficients fitted to the published extension
  EMC tables (A 2.870, B -0.00538, C 1.380, within 0.05 point of them).
  Both reproduce the tables: 60 F and 80% RH gives 16.1% corn, 18.3% beans.

  Corn moves slowly and predictably: an hourly first-order approach to
  equilibrium, drying at 0.10 of the gap per day - which reproduces Iowa
  State's and Purdue's measured 0.4-0.8 points a day - and rewetting at a
  quarter of that (Hurburgh, Iowa State). Iowa State's own field model
  misses corn by about 2 points, so that is the band shown.

  Soybeans cannot be predicted to the point. The same Iowa State model
  misses them by 6.7 points, more than the overnight swing itself. What is
  predictable is whether the pods get wet, and dew is: surfaces are wet
  when the forecast dew point is within 2 C of the air (3 C on a clear,
  calm night, when pods radiate below air temperature), and dry off above
  3.8 C (Lulu et al. 2008) or in wind of 2.5 m/s with RH under 87.8%
  (Gleason et al., Iowa State). So beans get a risk rating with a range,
  and a time they should be back to target - which is the question a
  morning start actually turns on.

Every constant is named in MODEL_JS so it can be recalibrated against the
farm's own combine readings once enough nights have been recorded.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, read_json  # noqa: E402

OUTPUT = SECRETS / "moisture-block.html"
RELAY = SECRETS / "relay.json"
FLEET = SECRETS / "fleet.json"

# ---------------------------------------------------------------------------
# The model. Pure functions, no DOM, so the tests can run exactly this code
# under Node. Nothing here may use a character above 127.

MODEL_JS = r"""
var GTM = (function () {
  'use strict';

  // --- equilibrium moisture content, % wet basis ------------------------
  var CORN = { A: 374.34, B: 0.18662, C: 31.696 };   // ASAE D245.6 Chung-Pfost
  var SOY = { A: 2.870, B: -0.00538, C: 1.380 };     // Halsey, fitted to EMC tables
  // The equations were fitted up to about 90-93% RH and head to infinity at
  // 100%. Above this the pods are wet, which the wetness model handles.
  var RH_MAX = 95, RH_MIN = 10;

  function emc(crop, tC, rhPct) {
    var rh = Math.min(RH_MAX, Math.max(RH_MIN, rhPct)) / 100;
    var t = Math.max(-20, tC), m;
    if (crop === 'soybeans') {
      m = Math.pow(-Math.exp(SOY.A + SOY.B * t) / Math.log(rh), 1 / SOY.C);
    } else {
      m = -(1 / CORN.B) * Math.log(-(t + CORN.C) * Math.log(rh) / CORN.A);
    }
    return 100 * m / (100 + m);                        // dry basis -> wet basis
  }

  // --- are the pods and husks wet this hour? ----------------------------
  var DEW_ON = 2.0;          // C: dew point depression at which dew forms
  var DEW_ON_CLEAR = 3.0;    // C: the same on a clear, calm night
  var DEW_OFF = 3.8;         // C: surfaces dry above this (Lulu et al. 2008)
  var CLEAR_SKY = 30;        // % sky cover
  var CALM = 2.5;            // m/s
  var WINDY_DRY_RH = 87.8;   // % - wind this strong with RH below this dries
  var RAIN_WET_MM = 0.25;    // mm in the hour (0.01 in)

  function wetness(hours) {
    var wet = false, out = [];
    for (var i = 0; i < hours.length; i++) {
      var h = hours[i], dpd = h.t - h.td;
      if (h.qpf >= RAIN_WET_MM) {
        wet = true; out.push('rain'); continue;
      }
      var onset = (h.sky < CLEAR_SKY && h.wind < CALM) ? DEW_ON_CLEAR : DEW_ON;
      if (!wet && dpd <= onset) wet = true;
      else if (wet && (dpd > DEW_OFF || (h.wind >= CALM && h.rh < WINDY_DRY_RH))) wet = false;
      out.push(wet ? 'dew' : null);
    }
    return out;
  }

  // --- tonight: from the first night hour to the next day hour -----------
  function nightWindow(hours) {
    var i0 = -1, i1 = -1, i;
    for (i = 0; i < hours.length; i++) { if (!hours[i].day) { i0 = i; break; } }
    if (i0 < 0) return null;
    for (i = i0; i < hours.length; i++) { if (hours[i].day) { i1 = i; break; } }
    if (i1 < 0) i1 = hours.length;
    return { i0: i0, i1: i1 };
  }

  // The next run of daytime hours after index `from`, to its sunset.
  function dayAfter(hours, from) {
    var s = -1, e = -1, i;
    for (i = from; i < hours.length; i++) { if (hours[i].day) { s = i; break; } }
    if (s < 0) return null;
    for (i = s; i < hours.length; i++) { if (!hours[i].day) { e = i; break; } }
    return { s: s, e: e < 0 ? hours.length : e };
  }

  function nightStats(hours, wet, w) {
    var st = { wetH: 0, dewH: 0, rainMm: 0, maxRh: 0, lowT: 99, lowDpd: 99,
               firstWet: -1, lastWet: -1, sky: 0, wind: 0, n: 0 };
    for (var i = w.i0; i < w.i1; i++) {
      var h = hours[i];
      st.n++; st.sky += h.sky; st.wind += h.wind;
      st.rainMm += h.qpf;
      st.maxRh = Math.max(st.maxRh, h.rh);
      if (h.t < st.lowT) st.lowT = h.t;
      st.lowDpd = Math.min(st.lowDpd, h.t - h.td);
      if (wet[i]) {
        st.wetH++; if (wet[i] === 'dew') st.dewH++;
        if (st.firstWet < 0) st.firstWet = i;
        st.lastWet = i;
      }
    }
    if (st.n) { st.sky /= st.n; st.wind /= st.n; }
    // dew often hangs on after sunrise; follow it into the morning
    var j = w.i1;
    while (st.lastWet === j - 1 && j < hours.length && wet[j]) { st.lastWet = j; j++; }
    return st;
  }

  // --- soybeans: a rating, not a number ----------------------------------
  var SOY_LEVELS = {
    low:      { gainLo: 0, gainHi: 1, text: 'little change overnight' },
    moderate: { gainLo: 1, gainHi: 3, text: 'about +1 to +3 points by dawn' },
    high:     { gainLo: 2, gainHi: 5, text: 'about +2 to +5 points by dawn' },
    rain:     { gainLo: null, gainHi: null, text: 'several points - usually more than a dewy night, and slower to dry back' }
  };
  var SOY_RAIN_MM = 2.5;      // 0.1 in overnight
  var SOY_HIGH_WET_H = 5;

  function soyRating(st) {
    if (st.rainMm >= SOY_RAIN_MM) return 'rain';
    if (st.wetH >= SOY_HIGH_WET_H || (st.rainMm > 0 && st.wetH >= 2)) return 'high';
    if (st.wetH >= 1) return 'moderate';
    return 'low';
  }

  // First hour tomorrow the beans should be back at or under target:
  // surfaces dry for `need` hours running and equilibrium at or below target,
  // then an hour for the beans to follow. Null if not before sunset.
  function soyBackAt(hours, wet, w, target, level) {
    var d = dayAfter(hours, w.i1);
    if (!d) return null;
    var need = level === 'rain' ? 4 : 1, run = 0;
    for (var i = d.s; i < d.e; i++) {
      var ok = !wet[i] && emc('soybeans', hours[i].t, hours[i].rh) <= target;
      run = ok ? run + 1 : 0;
      if (run >= need) return Math.min(i + 1, d.e - 1);
    }
    return null;
  }

  // --- corn: an hourly track, first order toward equilibrium --------------
  var K_DRY = 0.10;           // fraction of the gap closed per day, drying
  var REWET_FRACTION = 0.25;  // rewets at a quarter of the drying rate

  function cornTrack(hours, wet, m0) {
    var m = m0, track = [];
    for (var i = 0; i < hours.length; i++) {
      var h = hours[i];
      var me = emc('corn', h.t, wet[i] ? RH_MAX : h.rh);
      var k = (m > me ? K_DRY : K_DRY * REWET_FRACTION) / 24;
      m = m - k * (m - me);
      track.push(m);
    }
    return track;
  }

  // --- one call for the page ---------------------------------------------
  function analyse(hours, crop, m0, target) {
    var wet = wetness(hours);
    var w = nightWindow(hours);
    var res = { crop: crop, m0: m0, target: target, wet: wet, night: w };
    if (!w) return res;
    var st = nightStats(hours, wet, w);
    res.stats = st;
    res.tomorrow = dayAfter(hours, w.i1);
    if (crop === 'soybeans') {
      var level = soyRating(st);
      res.level = level;
      res.levelInfo = SOY_LEVELS[level];
      res.backAt = soyBackAt(hours, wet, w, target, level);
      res.emc = hours.map(function (h) { return emc('soybeans', h.t, h.rh); });
    } else {
      var tr = cornTrack(hours, wet, m0);
      res.track = tr;
      res.dawn = tr[Math.min(w.i1, tr.length - 1)];
      res.reachAt = -1;
      if (target < m0) {
        for (var i = 0; i < tr.length; i++) { if (tr[i] <= target) { res.reachAt = i; break; } }
      }
    }
    return res;
  }

  // Per local day: rain total, lowest daytime RH, and the crop's figure -
  // corn's modelled 4 PM moisture, or the beans' afternoon equilibrium.
  function days(hours, res) {
    var out = [], by = {};
    for (var i = 0; i < hours.length; i++) {
      var h = hours[i], k = h.dayKey;
      if (!by[k]) { by[k] = { key: k, rainMm: 0, minRh: 999, crop: null, cropAt: -1 }; out.push(by[k]); }
      var d = by[k];
      d.rainMm += h.qpf;
      if (h.day) d.minRh = Math.min(d.minRh, h.rh);
      if (res.track && h.hod === 16) { d.crop = res.track[i]; d.cropAt = i; }
      if (res.emc && h.day && !res.wet[i]) {
        if (d.crop === null || res.emc[i] < d.crop) { d.crop = res.emc[i]; d.cropAt = i; }
      }
    }
    return out;
  }

  // --- National Weather Service grid data ---------------------------------
  // "PT6H", "P1DT3H" -> hours
  function durationHours(iso) {
    var m = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$/.exec(iso || '');
    if (!m) return 1;
    return (+(m[1] || 0)) * 24 + (+(m[2] || 0)) + ((+(m[3] || 0)) >= 30 ? 1 : 0) || 1;
  }

  // One layer -> { hourMs: value }. Totals (rain) are spread evenly over
  // the hours they cover; everything else holds its value across them.
  function expand(layer, total) {
    var out = {};
    if (!layer || !layer.values) return out;
    layer.values.forEach(function (v) {
      if (v.value === null || v.value === undefined) return;   // a gap, not a zero
      var parts = v.validTime.split('/');
      var start = Date.parse(parts[0]), n = durationHours(parts[1]);
      for (var j = 0; j < n; j++) {
        out[start + j * 3600000] = total ? (v.value || 0) / n : v.value;
      }
    });
    return out;
  }

  var TZ = 'America/Chicago';
  var fmtHour = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hour: 'numeric', hourCycle: 'h23' });
  var fmtDay = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' });

  // grid: gridpoint properties; hourly: forecastHourly periods (for isDaytime
  // and the short text); nowMs: the hour to start from.
  function buildHours(grid, hourly, nowMs, maxHours) {
    var T = expand(grid.temperature), TD = expand(grid.dewpoint),
        RH = expand(grid.relativeHumidity), WS = expand(grid.windSpeed),
        SKY = expand(grid.skyCover), QPF = expand(grid.quantitativePrecipitation, true),
        POP = expand(grid.probabilityOfPrecipitation);
    var dayFlag = {}, text = {};
    (hourly || []).forEach(function (p) {
      var ms = Date.parse(p.startTime);
      dayFlag[ms] = !!p.isDaytime; text[ms] = p.shortForecast;
    });
    var start = Math.floor(nowMs / 3600000) * 3600000, out = [];
    for (var k = 0; k < (maxHours || 168); k++) {
      var ms = start + k * 3600000;
      if (T[ms] === undefined || TD[ms] === undefined || RH[ms] === undefined) {
        if (out.length) break; else continue;
      }
      var hod = +fmtHour.format(new Date(ms));
      out.push({
        ms: ms, hod: hod, dayKey: fmtDay.format(new Date(ms)),
        day: dayFlag[ms] !== undefined ? dayFlag[ms] : (hod >= 7 && hod < 19),
        t: T[ms], td: TD[ms], rh: RH[ms],
        wind: (WS[ms] || 0) / 3.6,                 // km/h -> m/s
        sky: SKY[ms] === undefined || SKY[ms] === null ? 50 : SKY[ms],
        qpf: QPF[ms] || 0, pop: POP[ms] || 0,
        text: text[ms] || ''
      });
    }
    return out;
  }

  // --- the best "moisture now" for a field ---------------------------------
  // Its own combine reading if it was harvested in the last day; otherwise
  // the nearest field of the same crop read in the last three days, within
  // 25 km. Returns {m, fieldId, km, end} or null.
  function pickReading(field, fields, readings, nowMs) {
    if (!readings) return null;
    var own = readings[field.id];
    if (own && own.m && nowMs - Date.parse(own.end) < 24 * 3600000) {
      return { m: own.m, fieldId: field.id, km: 0, end: own.end };
    }
    var best = null;
    fields.forEach(function (f) {
      var r = readings[f.id];
      if (!r || !r.m || f.c !== field.c) return;
      if (nowMs - Date.parse(r.end) > 72 * 3600000) return;
      var km = distKm(field.y, field.x, f.y, f.x);
      if (km > 25) return;
      if (!best || km < best.km) best = { m: r.m, fieldId: f.id, km: km, end: r.end };
    });
    return best;
  }

  function distKm(lat1, lon1, lat2, lon2) {
    var dy = (lat2 - lat1) * 111.32;
    var dx = (lon2 - lon1) * 111.32 * Math.cos((lat1 + lat2) / 2 * Math.PI / 180);
    return Math.sqrt(dx * dx + dy * dy);
  }

  return { emc: emc, wetness: wetness, nightWindow: nightWindow, dayAfter: dayAfter,
           nightStats: nightStats, soyRating: soyRating, soyBackAt: soyBackAt,
           cornTrack: cornTrack, analyse: analyse, days: days,
           durationHours: durationHours, expand: expand, buildHours: buildHours,
           pickReading: pickReading, distKm: distKm, SOY_LEVELS: SOY_LEVELS };
})();
if (typeof module !== 'undefined') module.exports = GTM;
"""

# ---------------------------------------------------------------------------
# The page.

UI_JS = r"""
(function () {
  var RELAY = __RELAY_URL__, TOKEN = __RELAY_TOKEN__;
  var FIELDS = __FIELDS__;   // [{id, n, c, y, x}]
  var $ = function (id) { return document.getElementById(id); };
  var DOT = ' ' + String.fromCharCode(183) + ' ';
  var DEG = String.fromCharCode(176);
  var feed = null, cache = {};
  var byId = {};
  FIELDS.forEach(function (f) { byId[f.id] = f; });

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function F(c) { return Math.round(c * 9 / 5 + 32); }
  function mph(ms) { return Math.round(ms * 2.23694); }
  function inch(mm) { return (mm / 25.4).toFixed(2); }
  function pct(v) { return (Math.round(v * 10) / 10).toFixed(1); }
  var fTime = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', hour: 'numeric' });
  var fDay = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', weekday: 'short' });
  var fDate = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', month: 'short', day: 'numeric' });
  function when(ms) { return fTime.format(new Date(ms)).replace(' ', '').toLowerCase(); }
  function whenDay(ms) { return fDay.format(new Date(ms)) + ' ' + when(ms); }

  // --- the field picker --------------------------------------------------
  function fillFields() {
    var sel = $('gtm-field'), groups = { corn: [], soybeans: [], other: [] };
    FIELDS.slice().sort(function (a, b) { return a.n.localeCompare(b.n); })
      .forEach(function (f) { (groups[f.c] || groups.other).push(f); });
    var html = '';
    [['corn', 'Corn'], ['soybeans', 'Soybeans'], ['other', 'Crop unknown']].forEach(function (g) {
      if (!groups[g[0]].length) return;
      html += '<optgroup label="' + g[1] + '">' + groups[g[0]].map(function (f) {
        return '<option value="' + esc(f.id) + '">' + esc(f.n) + '</option>';
      }).join('') + '</optgroup>';
    });
    sel.innerHTML = html;
  }

  function renderCombines() {
    var box = $('gtm-combines');
    if (!feed || !feed.combines) { box.innerHTML = ''; return; }
    var now = Date.now();
    var here = feed.combines.filter(function (c) {
      return c.field && byId[c.field] && now - Date.parse(c.at) < 3 * 3600000;
    });
    if (!here.length) { box.innerHTML = '<span class="gtm-note">No combine in a field right now.</span>'; return; }
    box.innerHTML = 'Combines now: ' + here.map(function (c) {
      return '<button type="button" class="gtm-chip" data-f="' + esc(c.field) + '">' +
        esc(byId[c.field].n) + ' <span class="gtm-note">' + esc(c.label) + '</span></button>';
    }).join(' ');
    Array.prototype.forEach.call(box.querySelectorAll('.gtm-chip'), function (b) {
      b.onclick = function () { $('gtm-field').value = b.getAttribute('data-f'); onField(); };
    });
  }

  function defaultField() {
    var now = Date.now(), best = null;
    ((feed && feed.combines) || []).forEach(function (c) {
      if (c.field && byId[c.field] && now - Date.parse(c.at) < 3 * 3600000 &&
          (!best || c.at > best.at)) best = c;
    });
    return best ? best.field : FIELDS[0] && FIELDS[0].id;
  }

  // --- moisture now ---------------------------------------------------------
  function onField() {
    var f = byId[$('gtm-field').value];
    if (!f) return;
    $('gtm-target').value = f.c === 'soybeans' ? '13' : '20';
    var r = GTM.pickReading(f, FIELDS, feed && feed.readings, Date.now());
    var note = $('gtm-mnote');
    if (r) {
      $('gtm-m').value = pct(r.m);
      note.innerHTML = r.km === 0
        ? 'Combine average in this field, through ' + esc(whenDay(Date.parse(r.end)))
        : 'Combine reading from ' + esc(byId[r.fieldId].n) + ', ' + Math.round(r.km * 0.621) +
          ' mi away, ' + esc(fDate.format(new Date(r.end))) + '. Edit if you have a better number.';
    } else {
      $('gtm-m').value = '';
      note.innerHTML = 'No recent combine reading nearby - enter a tester reading.';
    }
    run();
  }

  // --- the forecast -------------------------------------------------------------
  function getJSON(url, tries) {
    return fetch(url, { headers: { 'Accept': 'application/geo+json' } }).then(function (r) {
      if (r.ok) return r.json();
      if ((tries || 0) < 2 && r.status >= 500) {
        return new Promise(function (ok) { setTimeout(ok, 1200); })
          .then(function () { return getJSON(url, (tries || 0) + 1); });
      }
      throw new Error('weather service answered ' + r.status);
    });
  }

  function forecast(f) {
    var c = cache[f.id];
    if (c && Date.now() - c.at < 30 * 60000) return Promise.resolve(c);
    return getJSON('https://api.weather.gov/points/' + f.y.toFixed(4) + ',' + f.x.toFixed(4))
      .then(function (p) {
        return Promise.all([getJSON(p.properties.forecastGridData),
                            getJSON(p.properties.forecastHourly)]);
      }).then(function (both) {
        var out = { grid: both[0].properties, hourly: both[1].properties.periods, at: Date.now() };
        cache[f.id] = out;
        return out;
      });
  }

  // --- the answer ------------------------------------------------------------------
  function run() {
    var f = byId[$('gtm-field').value];
    var m0 = parseFloat($('gtm-m').value), target = parseFloat($('gtm-target').value);
    var out = $('gtm-out');
    if (!f) return;
    if (f.c !== 'corn' && f.c !== 'soybeans') {
      out.innerHTML = '<div class="gtm-verdict">Operations Center has no crop for this field this year.</div>';
      return;
    }
    if (!(m0 > 5 && m0 < 45)) {
      out.innerHTML = '<div class="gtm-verdict gtm-v-none">Enter what the ' + (f.c === 'corn' ? 'corn' : 'beans') +
        ' are testing now.</div>';
      return;
    }
    out.innerHTML = '<div class="gtm-note">Getting the forecast for this field' + String.fromCharCode(8230) + '</div>';
    forecast(f).then(function (fc) {
      var hours = GTM.buildHours(fc.grid, fc.hourly, Date.now(), 168);
      if (hours.length < 12) throw new Error('the forecast came back empty');
      var res = GTM.analyse(hours, f.c, m0, target);
      out.innerHTML = render(f, hours, res) + table(hours, res) + dayTable(hours, res) + foot(f, fc);
    }).catch(function (e) {
      out.innerHTML = '<div class="gtm-verdict gtm-v-none">Could not get the forecast: ' + esc(e.message) +
        '. Try again in a minute.</div>';
    });
  }

  function why(hours, res) {
    var st = res.stats, w = res.night;
    var parts = [];
    if (st.rainMm >= 0.25) parts.push(inch(st.rainMm) + ' in of rain');
    if (st.dewH) {
      parts.push('dew likely ' + (st.firstWet >= 0 ? when(hours[st.firstWet].ms) : '') +
        ' to ' + (st.lastWet >= 0 ? when(hours[Math.min(st.lastWet + 1, hours.length - 1)].ms) : ''));
    }
    parts.push('low ' + F(st.lowT) + DEG + 'F');
    parts.push(st.lowDpd < 0.6 ? 'air saturated at times (fog or drizzle)'
                               : 'dew point within ' + Math.round(st.lowDpd * 1.8) + DEG + 'F of the air');
    parts.push(st.sky < 30 ? 'mostly clear' : st.sky < 70 ? 'partly cloudy' : 'cloudy');
    parts.push('wind about ' + mph(st.wind) + ' mph');
    return parts.join(DOT);
  }

  function render(f, hours, res) {
    if (!res.night) return '<div class="gtm-verdict">No night in the forecast window.</div>';
    var h = '';
    if (f.c === 'soybeans') {
      var L = res.levelInfo, lv = res.level;
      h += '<div class="gtm-verdict gtm-v-' + lv + '">Tonight: ' + (lv === 'rain'
        ? 'rain is coming - the beans will pick up moisture'
        : { low: 'LOW', moderate: 'MODERATE', high: 'HIGH' }[lv] + ' chance the beans pick up moisture') + '</div>';
      // A range only where a source gives one: up to about 5 points on a
      // dewy night (Legume Hub). For rain the sources say only "several
      // points", so no number is put on it.
      h += '<div class="gtm-line">' + L.text + (lv === 'low' || lv === 'rain' ? '' :
        ' - from ' + pct(res.m0) + '% to roughly ' + pct(res.m0 + L.gainLo) + '-' + pct(res.m0 + L.gainHi) + '%') + '.</div>';
      if (res.backAt !== null && res.backAt !== undefined) {
        h += '<div class="gtm-line"><b>Likely back to ' + pct(res.target) + '% around ' +
          when(hours[res.backAt].ms) + ' ' + fDay.format(new Date(hours[res.backAt].ms)) + '.</b></div>';
      } else {
        h += '<div class="gtm-line"><b>May not get back to ' + pct(res.target) + '% tomorrow</b> - the air stays too damp.</div>';
      }
    } else {
      var gain = res.dawn - res.m0, rainy = res.stats.rainMm >= 2.5;
      // Rain is the one thing corn does take up, and nobody has measured how
      // much - so the track stays on the humidity physics and the page says
      // plainly that rain is coming rather than inventing a number for it.
      h += '<div class="gtm-verdict gtm-v-' + (rainy || gain > 0.3 ? 'moderate' : 'low') + '">Tonight: ' +
        (gain > 0.05 ? '+' + gain.toFixed(1) + ' points' : 'about the same') +
        ' - about ' + pct(res.dawn) + '% by ' + when(hours[Math.min(res.night.i1, hours.length - 1)].ms) +
        (rainy ? ', but rain is coming' : '') + '</div>';
      h += rainy
        ? '<div class="gtm-line"><b>' + inch(res.stats.rainMm) + ' in of rain tonight.</b> Corn in open husks can hold water, ' +
          'so expect little or no drying tomorrow morning - the number above does not count water caught in the ear.</div>'
        : '<div class="gtm-line">Corn barely rewets from dew; the husk keeps most of it off. Rain matters more than dew.</div>';
      if (res.target < res.m0) {
        h += '<div class="gtm-line"><b>' + (res.reachAt >= 0
          ? 'Reaches ' + pct(res.target) + '% around ' + whenDay(hours[res.reachAt].ms)
          : 'Not at ' + pct(res.target) + '% within the forecast') + '</b> (' + String.fromCharCode(177) + '2 points).</div>';
      }
    }
    h += '<div class="gtm-why">' + why(hours, res) + '</div>';
    return h;
  }

  function table(hours, res) {
    var end = Math.min(hours.length, (res.tomorrow ? res.tomorrow.e : res.night.i1) + 1, 40);
    var soy = res.crop === 'soybeans';
    var rows = '';
    for (var i = 0; i < end; i++) {
      var h = hours[i], w = res.wet[i];
      rows += '<tr class="' + (h.day ? 'gtm-day' : 'gtm-night') + (w ? ' gtm-wet' : '') + '">' +
        '<td>' + when(h.ms) + '</td><td>' + F(h.t) + DEG + '</td><td>' + F(h.td) + DEG + '</td>' +
        '<td>' + Math.round(h.rh) + '%</td><td>' + mph(h.wind) + '</td><td>' + Math.round(h.sky) + '%</td>' +
        '<td>' + (h.qpf >= 0.25 ? inch(h.qpf) : '') + '</td>' +
        '<td>' + (w === 'rain' ? 'rain' : w === 'dew' ? 'dew' : '') + '</td>' +
        '<td>' + pct(soy ? res.emc[i] : res.track[i]) + '</td></tr>';
    }
    return '<details class="gtm-det"><summary>Hour by hour</summary><div class="gtm-scroll"><table class="gtm-t">' +
      '<tr><th>Time</th><th>Temp</th><th>Dew pt</th><th>RH</th><th>Wind mph</th><th>Clouds</th><th>Rain in</th>' +
      '<th>Wet?</th><th>' + (soy ? 'Beans head to' : 'Corn') + '</th></tr>' + rows + '</table></div>' +
      (soy ? '<div class="gtm-note">"Beans head to" is equilibrium moisture: where dry beans settle in that air, not a prediction of the bean.</div>' : '') +
      '</details>';
  }

  function dayTable(hours, res) {
    var soy = res.crop === 'soybeans';
    var rows = GTM.days(hours, res).map(function (d) {
      var ms = Date.parse(d.key + 'T12:00:00-05:00');
      return '<tr><td>' + fDay.format(new Date(ms)) + ' ' + fDate.format(new Date(ms)) + '</td>' +
        '<td>' + (d.minRh < 999 ? Math.round(d.minRh) + '%' : '') + '</td>' +
        '<td' + (d.rainMm >= 6 ? ' class="gtm-rainy"' : '') + '>' + (d.rainMm >= 0.25 ? inch(d.rainMm) : '0') + '</td>' +
        '<td>' + (d.crop === null ? '' : pct(d.crop) + '%') + '</td></tr>';
    }).join('');
    return '<details class="gtm-det"' + (soy ? '' : ' open') + '><summary>The week</summary><table class="gtm-t">' +
      '<tr><th>Day</th><th>Lowest RH</th><th>Rain in</th><th>' +
      (soy ? 'Beans, driest afternoon hour' : 'Corn at 4 pm') + '</th></tr>' + rows + '</table></details>';
  }

  function foot(f, fc) {
    var up = fc.grid.updateTime ? whenDay(Date.parse(fc.grid.updateTime)) : '';
    return '<div class="gtm-foot">Forecast: National Weather Service for this field' + (up ? ', issued ' + esc(up) : '') + '. ' +
      (f.c === 'soybeans'
        ? 'Soybeans swing too fast to predict to the point - the best published model misses them by about 7 points - so this is a rating. ' +
          'Check a tester reading before you start.'
        : 'Corn estimate is good to about ' + String.fromCharCode(177) + '2 points over a day or two, less beyond that.') + '</div>';
  }

  function pullFeed() {
    if (!RELAY || !window.fetch) return Promise.resolve();
    return fetch(RELAY.replace(/\/+$/, '') + '/moisture', { cache: 'no-store',
      headers: { 'Authorization': 'Bearer ' + TOKEN } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { feed = d; $('gtm-asof').textContent = d && d.generated_at
        ? 'combines ' + whenDay(Date.parse(d.generated_at)) : ''; })
      .catch(function () { feed = null; });
  }

  fillFields();
  $('gtm-field').onchange = onField;
  $('gtm-m').onchange = run;
  $('gtm-target').onchange = run;
  pullFeed().then(function () {
    renderCombines();
    $('gtm-field').value = defaultField();
    onField();
  });
})();
"""

BLOCK = r"""<!-- Overnight grain moisture: private page only (field names and the relay's read token are inside). -->
<div id="gtm" class="gtm">
  <div class="gtm-head">
    <span class="gtm-title">Overnight grain moisture</span>
    <span class="gtm-asof" id="gtm-asof"></span>
  </div>
  <div id="gtm-combines" class="gtm-combines"></div>
  <div class="gtm-form">
    <label class="gtm-wide">Field<select id="gtm-field"></select></label>
    <label>Moisture now %<input id="gtm-m" type="number" step="0.1" min="5" max="45" inputmode="decimal"></label>
    <label>Target %<input id="gtm-target" type="number" step="0.5" min="8" max="35" inputmode="decimal"></label>
  </div>
  <div id="gtm-mnote" class="gtm-note"></div>
  <div id="gtm-out"></div>
</div>
<style>
  .gtm { font: 15px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #1c2a1e;
         max-width: 720px; margin: 0 auto; }
  .gtm-head { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap;
              border-bottom: 2px solid #2e6b3a; padding-bottom: 6px; margin-bottom: 10px; gap: 8px; }
  .gtm-title { font-size: 20px; font-weight: 700; }
  .gtm-asof, .gtm-note { font-size: 13px; color: #5b6b5e; }
  .gtm-combines { margin-bottom: 10px; font-size: 14px; }
  .gtm-chip { font: inherit; font-size: 13px; border: 1px solid #b9cbbc; background: #eef5ef; color: #1c2a1e;
              border-radius: 14px; padding: 3px 10px; margin: 2px 2px 2px 0; cursor: pointer; }
  .gtm-form { display: flex; flex-wrap: wrap; gap: 10px; }
  .gtm-form label { display: flex; flex-direction: column; font-size: 13px; color: #445247; flex: 1 1 110px; }
  .gtm-form .gtm-wide { flex: 3 1 240px; }
  .gtm-form select, .gtm-form input { font: inherit; font-size: 16px; padding: 7px 8px; margin-top: 3px;
                                      border: 1px solid #b9cbbc; border-radius: 8px; background: #fff; color: #1c2a1e;
                                      min-width: 0; width: 100%; box-sizing: border-box; }
  #gtm-mnote { margin: 6px 0 12px; }
  .gtm-verdict { font-size: 20px; font-weight: 800; margin: 6px 0 4px; }
  .gtm-v-low { color: #2e6b3a; } .gtm-v-moderate { color: #b7791f; }
  .gtm-v-high, .gtm-v-rain { color: #b3261e; } .gtm-v-none { color: #5b6b5e; font-size: 16px; font-weight: 600; }
  .gtm-line { margin: 3px 0; }
  .gtm-why { font-size: 13px; color: #445247; margin: 8px 0 10px; }
  .gtm-det { margin: 8px 0; border: 1px solid #d5ddd6; border-radius: 8px; padding: 6px 10px; background: #fafcfa; }
  .gtm-det summary { cursor: pointer; font-weight: 600; }
  .gtm-scroll { overflow-x: auto; }
  .gtm-t { border-collapse: collapse; font-size: 13px; margin: 8px 0; width: 100%; }
  .gtm-t th, .gtm-t td { padding: 3px 6px; text-align: right; white-space: nowrap; border-bottom: 1px solid #e3e9e4; }
  .gtm-t th:first-child, .gtm-t td:first-child { text-align: left; }
  .gtm-t .gtm-night td { background: #f1f4f8; }
  .gtm-t .gtm-wet td { background: #e2ecf8; }
  .gtm-t td.gtm-rainy { color: #b3261e; font-weight: 700; }
  .gtm-foot { font-size: 12px; color: #6b776d; margin-top: 10px; }
</style>
<script>
__MODEL__
__UI__
</script>
"""


def field_list(fleet: dict) -> list[dict]:
    """The fields the page needs: id, name, crop, centroid. No outlines -
    the forecast is per 1.5-mile grid square, so a centroid is exact enough."""
    out = []
    for f in fleet.get("fields") or []:
        if f.get("lat") is None or f.get("lon") is None:
            continue
        out.append({"id": f["id"], "n": f.get("name") or f["id"],
                    "c": f.get("crop") or "", "y": round(f["lat"], 5), "x": round(f["lon"], 5)})
    return out


def build(url: str, token: str, fields: list[dict]) -> str:
    ui = (UI_JS.replace("__RELAY_URL__", json.dumps(url))
               .replace("__RELAY_TOKEN__", json.dumps(token))
               .replace("__FIELDS__", json.dumps(fields, separators=(",", ":"))))
    return BLOCK.replace("__MODEL__", MODEL_JS.strip()).replace("__UI__", ui.strip())


def main() -> None:
    cfg = read_json(RELAY, {})
    if not cfg.get("url") or not cfg.get("read_token"):
        sys.exit(f"{RELAY} needs \"url\" and \"read_token\"")
    fleet = read_json(FLEET, {})
    fields = field_list(fleet)
    if not fields:
        sys.exit(f"no fields in {FLEET} - run dev/jd_fleet.py first")
    html = build(cfg["url"], cfg["read_token"], fields)
    # Field names reach the page through json.dumps, which escapes anything
    # above 127 as \\uXXXX, so a non-ASCII character here is in the literal
    # page text - a bug to fix, not something to paper over.
    if any(ord(c) > 127 for c in html):
        sys.exit("refusing to write: the result is not pure ASCII")
    OUTPUT.write_text(html, encoding="ascii", newline="\n")
    crops = {}
    for f in fields:
        crops[f["c"] or "unknown"] = crops.get(f["c"] or "unknown", 0) + 1
    print(f"wrote {OUTPUT}  ({OUTPUT.stat().st_size / 1024:,.0f} KB)")
    print(f"  {len(fields)} fields: " + ", ".join(f"{n} {c}" for c, n in sorted(crops.items())))
    print()
    print("  Paste this into a Code block on a PASSWORD-PROTECTED Squarespace page.")
    print("  It carries field names and the relay's read token - never a public page.")


if __name__ == "__main__":
    main()
