"""Build a private copy of the hauling map with your fields in it.

    python dev/jd_build_private_map.py [--outlines]

Reads ~/.grain-map-secrets/fleet.json and writes

    %USERPROFILE%\\.grain-map-secrets\\private-map.html

which goes on a PASSWORD-PROTECTED Squarespace page. The field data is baked
into the HTML rather than fetched, which is what makes the page password
actually protect it - a password on a Squarespace page cannot protect a file
served from somewhere else.

Output goes outside the repo on purpose. This repo is public.

By default only each field's name, centroid and acres are embedded - about
20 KB, and everything the map needs to price a haul from that field. Pass
--outlines to include the boundary shapes as well: Deere's geometry
simplified to about a metre and polyline-encoded, with farmstead cutouts and
waterways carried as holes rather than filled in.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_trail import TRAIL_HOURS  # noqa: E402

SECRETS = pathlib.Path.home() / ".grain-map-secrets"
FLEET = SECRETS / "fleet.json"
OUTPUT = SECRETS / "private-map.html"
SOURCE = pathlib.Path(__file__).resolve().parent.parent / "grain-trucking-map.html"

# The field data is a top-level var, so it goes with the other configuration
# near the top of the file.
DATA_MARKER = "  // ====== Scraped cash bids ======"

# The picker script has to run INSIDE the widget's main function, where
# `container`, `map` and placeOrMoveTempPin exist. Putting it with the data
# threw "container is not defined" and failed silently.
SETUP_MARKER = "  setupCompanyFilter();\n  loadBids();"


def build_truck_js(trucks: list[dict]) -> str:
    """Road vehicles with a position, as a compact JS array.

    The report time goes in as-is: a truck that reported ten minutes ago and
    one that reported last week must not look the same on the map.
    """
    rows = []
    for t in trucks:
        if t.get("lat") is None or t.get("lon") is None:
            continue
        rows.append("{" + ",".join([
            "n:" + json.dumps(str(t.get("name") or "")),
            "m:" + json.dumps(str(t.get("make") or "")),
            "k:" + json.dumps(str(t.get("kind") or "")),
            f"y:{round(float(t['lat']), 6)}",
            f"x:{round(float(t['lon']), 6)}",
            "t:" + json.dumps(str(t.get("at") or "")),
            # s: when it stopped where it is. q: that is a lower bound, not a
            # known arrival. g: moving. e: engine running while stopped.
            "s:" + json.dumps(str(t.get("since") or "")),
            "q:" + ("1" if t.get("since_min") else "0"),
            "g:" + ("1" if t.get("moving") else "0"),
            "e:" + ("1" if t.get("engine_on") else "0"),
            # p: the path driven in the last 24 h, for the map to draw.
            "p:" + json.dumps([[round(a, 5), round(b, 5)]
                               for a, b in (t.get("trail") or [])],
                              separators=(",", ":")),
        ]) + "}")
    return "  var gtTrucks = [\n    " + ",\n    ".join(rows) + "\n  ];\n"


def build_stops_js(stops: list[dict], threshold: float) -> str:
    """Stops longer than the threshold, newest first."""
    rows = []
    for s in stops:
        rows.append("{" + ",".join([
            "n:" + json.dumps(str(s.get("name") or "")),
            "k:" + json.dumps(str(s.get("kind") or "")),
            "a:" + json.dumps(str(s.get("start") or "")),
            "z:" + json.dumps(str(s.get("end") or "")),
            f"m:{int(s.get('minutes') or 0)}",
            f"y:{round(float(s.get('y') or 0), 6)}",
            f"x:{round(float(s.get('x') or 0), 6)}",
        ]) + "}")
    return ("  var gtStops = [\n    " + ",\n    ".join(rows) + "\n  ];\n"
            + f"  var gtStopMinutes = {int(threshold)};\n")


def signed_area(points: list[list[float]]) -> float:
    """Shoelace on (lon, lat): negative is clockwise, the outer-ring sense."""
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        total += lon1 * lat2 - lon2 * lat1
    return total / 2.0


def encode_ring(points: list[list[float]]) -> str:
    """Google's polyline encoding, at six decimals rather than its usual five.

    Five decimals is a 1.1 m grid and puts faint steps on curved edges; six
    is 0.11 m. Each vertex costs a few characters instead of the twenty-odd
    of a JSON pair, and the output is plain ASCII (characters 63 to 126).
    """
    out = []
    prev_lat = prev_lon = 0
    for lat, lon in points:
        ilat, ilon = round(lat * 1e6), round(lon * 1e6)
        for v in (ilat - prev_lat, ilon - prev_lon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def build_field_js(fields: list[dict], outlines: bool) -> str:
    rows = []
    for f in fields:
        if f.get("lat") is None or f.get("lon") is None:
            continue
        name = json.dumps(str(f.get("name") or ""))
        acres = f.get("acres")
        parts = [f"n:{name}", f"y:{round(f['lat'], 6)}", f"x:{round(f['lon'], 6)}"]
        if acres:
            # Two decimals, as Operations Center shows it. Rounded here, at
            # the end, from Deere's full-precision figure.
            parts.append(f"a:{round(acres, 2)}")
        if outlines and f.get("rings"):
            # Each ring is its type letter (e = exterior, i = interior hole)
            # followed by the encoded vertices. Wound the shapefile way -
            # exterior clockwise, holes counter-clockwise - which Deere's own
            # output does not keep to: 54 exteriors in one export came back
            # counter-clockwise. The type label is what decides hole or not;
            # the winding is just made consistent for the drawn copy.
            rings = []
            for ring in f["rings"]:
                pts = ring["p"]
                if (ring["t"] == "i") != (signed_area(pts) > 0):
                    pts = pts[::-1]
                rings.append(ring["t"] + encode_ring(pts))
            parts.append("r:" + json.dumps(rings, separators=(",", ":")))
        rows.append("{" + ",".join(parts) + "}")
    return "  var gtFields = [\n    " + ",\n    ".join(rows) + "\n  ];\n"


PANEL_CSS = """
  #grain-trucking-tool .gt-field-panel {
    margin: 0 0 14px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--surface);
    padding: 12px 14px;
  }
  #grain-trucking-tool .gt-field-head {
    display: flex; align-items: baseline; justify-content: space-between;
    flex-wrap: wrap; gap: 8px; margin-bottom: 8px;
  }
  #grain-trucking-tool .gt-field-title {
    font-family: 'Roboto Slab', Georgia, serif;
    font-size: 14px; font-weight: 700; color: var(--gold-deep);
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  #grain-trucking-tool .gt-field-count {
    font-size: 11.5px; color: var(--ink-soft);
    font-family: 'IBM Plex Mono', monospace;
  }
  #grain-trucking-tool .gt-field-search {
    width: 100%; box-sizing: border-box; padding: 9px 11px;
    border: 1px solid var(--line); border-radius: 8px;
    font-family: inherit; font-size: 14px; color: var(--ink);
  }
  #grain-trucking-tool .gt-field-list {
    margin-top: 8px; max-height: 230px; overflow-y: auto;
    border: 1px solid var(--line); border-radius: 8px;
  }
  #grain-trucking-tool .gt-field-list[hidden] { display: none; }
  #grain-trucking-tool .gt-field-item {
    display: flex; justify-content: space-between; gap: 10px;
    padding: 8px 11px; cursor: pointer; font-size: 13px;
    border-bottom: 1px solid var(--line);
  }
  #grain-trucking-tool .gt-field-item:last-child { border-bottom: none; }
  #grain-trucking-tool .gt-field-item:hover { background: var(--green-pale); }
  #grain-trucking-tool .gt-field-item-on { background: var(--green-pale); }
  #grain-trucking-tool .gt-stop-head {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 8px; margin: 11px 0 5px; padding-top: 9px;
    border-top: 1px solid var(--line);
  }
  #grain-trucking-tool .gt-stop-list {
    max-height: 148px; overflow-y: auto;
  }
  #grain-trucking-tool .gt-stop-item {
    display: flex; justify-content: space-between; gap: 10px;
    padding: 5px 7px; border-radius: 5px; font-size: 12px; cursor: pointer;
  }
  #grain-trucking-tool .gt-stop-item:hover { background: var(--surface-2, #efeae0); }
  #grain-trucking-tool .gt-stop-mins { color: #C0392B; font-weight: 600; flex: none; }
  #grain-trucking-tool .gt-stop-when { color: #5B6350; flex: none; }
  #grain-trucking-tool .gt-stop-none { padding: 5px 7px; font-size: 12px; color: #5B6350; }
  #grain-trucking-tool .gt-truck-key {
    display: inline-flex; align-items: center; gap: 4px;
    margin-right: 10px; white-space: nowrap;
  }
  #grain-trucking-tool .gt-truck-key i {
    width: 9px; height: 9px; border-radius: 2px;
    border: 1px solid rgba(255,255,255,0.85); flex: none;
  }
  #grain-trucking-tool .gt-field-acres {
    font-family: 'IBM Plex Mono', monospace; color: var(--ink-soft);
    white-space: nowrap;
  }
  #grain-trucking-tool .gt-field-note {
    font-size: 11.5px; color: var(--ink-soft); margin-top: 7px; line-height: 1.5;
  }
  #grain-trucking-tool .gt-field-toggle {
    display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
    font-size: 12px; color: var(--ink-soft); margin-top: 9px;
  }
  #grain-trucking-tool .gt-field-hint {
    cursor: default; font-size: 11px; color: var(--ink-soft);
    text-transform: uppercase; letter-spacing: 0.05em; background: var(--bg);
  }
  #grain-trucking-tool .gt-field-hint:hover { background: var(--bg); }
"""

PANEL_HTML = """
  <div class="gt-field-panel" id="gt-field-panel">
    <div class="gt-field-head">
      <span class="gt-field-title">Haul from one of your fields</span>
      <span class="gt-field-count" id="gt-field-count"></span>
    </div>
    <input type="text" class="gt-field-search" id="gt-field-search"
           placeholder="Type a field name to search&hellip;" autocomplete="off">
    <div class="gt-field-list" id="gt-field-list" hidden></div>
    <label class="gt-field-toggle">
      <input type="checkbox" id="gt-field-outlines" checked>
      Show field outlines on the map
    </label>
    <div class="gt-field-note" id="gt-field-note">Pick a field &mdash; by name or
      by clicking its outline &mdash; to use it as your loadout point. Every haul
      cost and the best-bids table below are then calculated from there.</div>
    <div class="gt-field-head" style="margin-top:12px;padding-top:11px;border-top:1px solid var(--line);">
      <label class="gt-field-toggle" style="margin-top:0;">
        <input type="checkbox" id="gt-truck-toggle" checked>
        Show trucks on the map
      </label>
      <span class="gt-field-count" id="gt-truck-summary"></span>
    </div>
    <div class="gt-field-note">
      <span class="gt-truck-key"><i style="background:#3F8F3F"></i>moving</span>
      <span class="gt-truck-key"><i style="background:#D2901F"></i>idling, engine on</span>
      <span class="gt-truck-key"><i style="background:#C0392B"></i>stopped</span>
      <span class="gt-truck-key"><i style="background:#98A08C"></i>not reporting</span>
      <br>Click a truck for how long it has sat, worked out from its position
      history. That is time since it last <em>moved</em>: Deere holds no
      engine data for these trucks &mdash; every one reports its engine as
      never having run &mdash; so a truck shut off and one idling look alike.
      &ldquo;At least&rdquo; means the history ran out before it found the
      truck somewhere else. Positions are each vehicle&rsquo;s last report,
      roughly ten to twenty minutes behind. Click a truck to draw the path it
      drove in the last 24 hours.</div>
    <div class="gt-stop-head">
      <span class="gt-field-title" style="font-size:12px">Long stops</span>
      <span class="gt-field-count" id="gt-stop-count"></span>
    </div>
    <div class="gt-stop-list" id="gt-stop-list"></div>
  </div>
"""

PANEL_JS = r"""
  // ====== Your fields (private) ======
  // Baked into this page rather than fetched, so the page password protects
  // it. Selecting a field drops the origin pin on its centroid, which is what
  // every haul cost and the best-bids ranking are measured from.
  (function setupFieldPicker() {
    var panel = container.querySelector('#gt-field-panel');
    var search = container.querySelector('#gt-field-search');
    var list = container.querySelector('#gt-field-list');
    var count = container.querySelector('#gt-field-count');
    var note = container.querySelector('#gt-field-note');
    if (!panel || typeof gtFields === 'undefined') return;

    var acresTotal = gtFields.reduce(function (t, f) { return t + (f.a || 0); }, 0);
    count.textContent = gtFields.length + ' fields \u00b7 ' +
      acresTotal.toLocaleString(undefined, { maximumFractionDigits: 0 }) + ' acres';

    function choose(f) {
      // Wait for the map: the picker renders before Google's script lands.
      if (!window.google || !window.google.maps || typeof map === 'undefined' || !map) {
        note.textContent = 'The map is still loading \u2014 try again in a moment.';
        return;
      }
      var pos = new google.maps.LatLng(f.y, f.x);
      placeOrMoveTempPin(pos, f.n);
      map.panTo(pos);
      if (map.getZoom() < 11) map.setZoom(11);
      Array.prototype.forEach.call(list.children, function (el) {
        el.classList.remove('gt-field-item-on');
      });
      note.textContent = 'Hauling from ' + f.n +
        (f.a ? ' (' + f.a.toFixed(2) + ' acres)' : '') +
        ' \u2014 now pick an elevator, or use the best-bids table below.';
      list.hidden = true;
      search.value = f.n;
    }

    function milesBetween(aLat, aLng, bLat, bLng) {
      var toRad = Math.PI / 180;
      var dLat = (bLat - aLat) * toRad, dLng = (bLng - aLng) * toRad;
      var s = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
        Math.cos(aLat * toRad) * Math.cos(bLat * toRad) *
        Math.sin(dLng / 2) * Math.sin(dLng / 2);
      return 2 * 3958.8 * Math.asin(Math.min(1, Math.sqrt(s)));
    }

    // With 223 fields, an empty box that shows nothing means you have to
    // already know a name to begin. Showing the ones nearest whatever the map
    // is looking at makes it usable without typing.
    function nearestToView(limit) {
      var centre = null;
      if (typeof map !== 'undefined' && map && map.getCenter) {
        var c = map.getCenter();
        if (c) centre = { lat: c.lat(), lng: c.lng() };
      }
      if (!centre) return gtFields.slice(0, limit);
      return gtFields.slice().sort(function (a, b) {
        return milesBetween(centre.lat, centre.lng, a.y, a.x) -
               milesBetween(centre.lat, centre.lng, b.y, b.x);
      }).slice(0, limit);
    }

    function render(query) {
      var q = String(query || '').trim().toLowerCase();
      list.innerHTML = '';
      var hits;
      if (!q) {
        hits = nearestToView(12);
        var head = document.createElement('div');
        head.className = 'gt-field-item gt-field-hint';
        head.textContent = 'Nearest to the map view - or start typing a name';
        list.appendChild(head);
      } else {
        // Names that start with what was typed are what you meant; the rest
        // are still worth offering, just below them.
        var starts = [], contains = [];
        gtFields.forEach(function (f) {
          var n = f.n.toLowerCase();
          if (n.indexOf(q) === 0) starts.push(f);
          else if (n.indexOf(q) !== -1) contains.push(f);
        });
        hits = starts.concat(contains).slice(0, 40);
      }
      if (!hits.length) {
        var none = document.createElement('div');
        none.className = 'gt-field-item';
        none.textContent = 'No field matches \u201c' + query + '\u201d';
        list.appendChild(none);
      }
      hits.forEach(function (f) {
        var row = document.createElement('div');
        row.className = 'gt-field-item';
        var name = document.createElement('span');
        name.textContent = f.n;
        var acres = document.createElement('span');
        acres.className = 'gt-field-acres';
        acres.textContent = f.a ? f.a.toFixed(2) + ' ac' : '';
        row.appendChild(name);
        row.appendChild(acres);
        row.addEventListener('click', function () { choose(f); });
        list.appendChild(row);
      });
      list.hidden = false;
    }

    search.addEventListener('input', function () { render(search.value); });
    search.addEventListener('focus', function () { render(''); });
    document.addEventListener('click', function (e) {
      if (!panel.contains(e.target)) list.hidden = true;
    });

    // ---- outlines -------------------------------------------------------
    // Drawn once the map exists. Clicking a field selects it exactly as the
    // search list does, so the shape on the map and the name in the list are
    // the same control.
    // Rings arrive polyline-encoded at six decimals (see encode_ring in the
    // generator). Same scheme as Google's own, with 1e6 in place of 1e5.
    function decodeRing(s) {
      var pts = [], i = 0, lat = 0, lng = 0;
      while (i < s.length) {
        for (var k = 0; k < 2; k++) {
          var result = 0, shift = 0, b;
          do {
            b = s.charCodeAt(i++) - 63;
            result |= (b & 0x1f) << shift;
            shift += 5;
          } while (b >= 0x20);
          var d = (result & 1) ? ~(result >> 1) : (result >> 1);
          if (k === 0) lat += d; else lng += d;
        }
        pts.push({ lat: lat / 1e6, lng: lng / 1e6 });
      }
      return pts;
    }

    var drawn = [];
    function drawOutlines() {
      if (drawn.length || typeof map === 'undefined' || !map) return;
      if (!window.google || !window.google.maps) return;
      gtFields.forEach(function (f) {
        if (!f.r || !f.r.length) return;
        // One polygon per field with every ring as a path. Google fills
        // multi-path polygons even-odd, so an interior ring is a hole - a
        // farmstead cutout or a waterway shows as bare imagery instead of
        // being painted over. The rings also keep Deere's orientation
        // (exterior clockwise, holes counter-clockwise) for any renderer
        // that goes by winding instead.
        var poly = new google.maps.Polygon({
          paths: f.r.map(function (ring) { return decodeRing(ring.slice(1)); }),
          strokeColor: '#9C6E1C', strokeOpacity: 0.9, strokeWeight: 1.5,
          fillColor: '#C08A28', fillOpacity: 0.18,
          map: map, zIndex: 1, clickable: true
        });
        poly.addListener('click', function () { choose(f); });
        poly.addListener('mouseover', function () {
          poly.setOptions({ fillOpacity: 0.35 });
        });
        poly.addListener('mouseout', function () {
          poly.setOptions({ fillOpacity: 0.18 });
        });
        drawn.push(poly);
      });
    }

    // gtInitMap may not have run yet when this executes.
    var tries = 0;
    (function waitForMap() {
      if (drawn.length) return;
      drawOutlines();
      if (!drawn.length && tries++ < 60) setTimeout(waitForMap, 500);
    })();

    var toggle = container.querySelector('#gt-field-outlines');
    if (toggle) {
      toggle.addEventListener('change', function () {
        drawn.forEach(function (p) { p.setMap(toggle.checked ? map : null); });
      });
    }
  })();

  // ====== Your trucks (private) ======
  // Positions come from Deere's ISO 15143-3 fleet feed, pushed by the farm PC
  // to a token-gated Cloudflare Worker. Deere republishes that feed about
  // every fifteen minutes - measured, not assumed - so a position is typically
  // ten to twenty minutes old. Every marker states its age rather than
  // implying it is current, because a parked truck stops reporting entirely.
  //
  // GT_FLEET_URL and GT_FLEET_TOKEN are only ever written into the private,
  // password-protected page. The token grants read access to truck positions
  // and nothing else - it cannot write - but it must never reach a public page.
  (function refreshTrucksFromRelay() {
    if (typeof GT_FLEET_URL === 'undefined' || !GT_FLEET_URL) return;
    if (!window.fetch) return;

    function pull() {
      fetch(GT_FLEET_URL, {
        cache: 'no-store',
        headers: { 'Authorization': 'Bearer ' + GT_FLEET_TOKEN }
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data || !data.trucks || !data.trucks.length) return;
          // The relay carries the same shape the generator bakes in, so the
          // drawing code below does not care which one it got.
          gtTrucks = data.trucks.map(function (t) {
            return { n: t.name, m: t.make, k: t.kind, y: t.lat, x: t.lon,
                     t: t.at, s: t.since, q: t.since_min ? 1 : 0,
                     g: t.moving ? 1 : 0, e: t.engine_on ? 1 : 0,
                     p: t.trail || [] };
          });
          if (data.stops) {
            gtStops = data.stops.map(function (s) {
              return { n: s.name, k: s.kind, a: s.start, z: s.end,
                       m: s.minutes, y: s.y, x: s.x };
            });
            if (data.stop_minutes) gtStopMinutes = data.stop_minutes;
          }
          if (window.gtRedrawTrucks) window.gtRedrawTrucks();
          if (window.gtRenderStops) window.gtRenderStops();
        })
        .catch(function () { /* keep whatever was baked in */ });
    }

    pull();
    // Deere's snapshot moves every ~15 minutes; five is frequent enough to
    // catch a new one promptly and still mostly return unchanged data.
    setInterval(pull, 5 * 60 * 1000);
  })();

  (function setupTrucks() {
    if (typeof gtTrucks === 'undefined' || !gtTrucks.length) return;
    var box = container.querySelector('#gt-truck-toggle');
    var summary = container.querySelector('#gt-truck-summary');
    var markers = [];

    function ageMinutes(iso) {
      if (!iso) return null;
      var t = Date.parse(iso);
      return isNaN(t) ? null : (Date.now() - t) / 60000;
    }

    function ageText(mins) {
      if (mins === null) return 'no report time';
      if (mins < 90) return Math.round(mins) + ' min ago';
      if (mins < 60 * 48) return (mins / 60).toFixed(1) + ' h ago';
      return Math.round(mins / 1440) + ' days ago';
    }

    function durationText(mins) {
      if (mins === null) return 'unknown';
      if (mins < 1) return 'under a minute';
      if (mins < 60) return Math.round(mins) + ' min';
      var h = Math.floor(mins / 60), m = Math.round(mins % 60);
      if (h < 24) return h + ' h' + (m ? ' ' + m + ' min' : '');
      var d = Math.floor(h / 24), rh = h % 24;
      return d + (d === 1 ? ' day' : ' days') + (rh ? ' ' + rh + ' h' : '');
    }

    // Four states, which is as much as the feed can honestly support.
    // "stopped" means the position has not changed - not that the engine is
    // off, which these trackers do not report. "idling" is the exception:
    // operating hours climbing on a truck that has not moved does mean the
    // engine is running.
    //
    // An old report is NOT stale. These trackers sleep while parked, so a
    // timestamp frozen three days ago means the truck has sat in the yard
    // for three days - the position is exactly right. Grey is only for a
    // tracker that has gone quiet long enough that the truck has probably
    // moved without it.
    var QUIET_DAYS = 14;

    function stateOf(t) {
      var reported = ageMinutes(t.t);
      if (reported === null || reported > 60 * 24 * QUIET_DAYS) return 'stale';
      if (t.g) return 'moving';
      if (t.e) return 'idling';
      return 'stopped';
    }

    var STATE_COLOUR = {
      moving:  '#3F8F3F',   // on the road
      idling:  '#D2901F',   // engine running, not going anywhere
      stopped: '#C0392B',   // parked
      stale:   '#98A08C'    // no report in over a day, position unreliable
    };

    // Side-view silhouettes, so a truck reads as a truck against the 896
    // elevator dots: a tractor and box for the Macks and Kenworths, a pickup
    // for the duallys and half-tons. Drawn here rather than taken from
    // Operations Center - these are my own shapes in the same style.
    var SEMI = '<path d="M3 16.5V11h5l3.5-5.5H19v11z"/>' +
               '<path d="M20.5 16.5V4H39v12.5z"/>';
    var SEMI_GLASS = '<path d="M12.6 10.4l2-3.5H18v3.5z"/>';
    var SEMI_WHEELS = '<circle cx="8.5" cy="17.5" r="3.3"/>' +
                      '<circle cx="27" cy="17.5" r="3.3"/>' +
                      '<circle cx="34.5" cy="17.5" r="3.3"/>';
    var PICKUP = '<path d="M2 14.5V10h4l3.5-5.5H18l2 5h11v5z"/>';
    var PICKUP_GLASS = '<path d="M10.5 9.6l2.1-3.8h4.6l.6 3.8z"/>';
    var PICKUP_WHEELS = '<circle cx="8" cy="15.5" r="3"/>' +
                        '<circle cx="25" cy="15.5" r="3"/>';

    function truckIcon(t) {
      var semi = t.k !== 'pickup';
      var w = semi ? 42 : 34, h = semi ? 22 : 20;
      var colour = STATE_COLOUR[stateOf(t)];
      // A white outline keeps the shape legible over dark aerial imagery;
      // the body is stroked before it is filled so the outline sits outside.
      var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + w +
        '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">' +
        '<g fill="' + colour + '" stroke="#FFFFFF" stroke-width="1.7" ' +
        'stroke-linejoin="round" paint-order="stroke">' +
        (semi ? SEMI : PICKUP) + (semi ? SEMI_WHEELS : PICKUP_WHEELS) +
        '</g>' +
        '<g fill="#2A3124" fill-opacity="0.55">' +
        (semi ? SEMI_GLASS : PICKUP_GLASS) + '</g>' +
        '<g fill="#2A3124">' +
        (semi ? SEMI_WHEELS : PICKUP_WHEELS) + '</g>' +
        '<g fill="#FFFFFF" fill-opacity="0.9">' +
        (semi ? '<circle cx="8.5" cy="17.5" r="1.1"/>' +
                '<circle cx="27" cy="17.5" r="1.1"/>' +
                '<circle cx="34.5" cy="17.5" r="1.1"/>'
              : '<circle cx="8" cy="15.5" r="1"/>' +
                '<circle cx="25" cy="15.5" r="1"/>') + '</g>' +
        '</svg>';
      return {
        url: 'data:image/svg+xml;charset=UTF-8,' + encodeURIComponent(svg),
        scaledSize: new google.maps.Size(w, h),
        // Anchor under the wheels so the truck sits on its position.
        anchor: new google.maps.Point(w / 2, h - 1)
      };
    }

    // "Moving" first, then whatever has been sitting the shortest time: in a
    // yard with a dozen trucks in it, the one that just arrived is the one
    // worth seeing on top.
    function stoppedMinutes(t) {
      return t.s ? ageMinutes(t.s) : ageMinutes(t.t);
    }

    function stoppedText(t) {
      var state = stateOf(t);
      if (state === 'moving') return 'moving';
      var mins = stoppedMinutes(t);
      if (mins === null) return 'stopped, for how long is unknown';
      var word = state === 'idling' ? 'idling' : 'stopped';
      // A lower bound is said as one. The pusher can only prove a truck has
      // been parked since it started watching; the truck may have been
      // there far longer.
      return word + ' ' + (t.q ? 'at least ' : '') + durationText(mins);
    }

    // ---- trail ----------------------------------------------------------
    // One truck's path at a time. Every trail at once is 24 hours of
    // breadcrumbs from a dozen vehicles over the same few roads, which
    // reads as scribble; clicking the truck you care about does not.
    var trail = null, trailHead = null, trailFor = null;

    function clearTrail() {
      if (trail) { trail.setMap(null); trail = null; }
      if (trailHead) { trailHead.setMap(null); trailHead = null; }
      trailFor = null;
    }

    function showTrail(t) {
      clearTrail();
      if (!t.p || t.p.length < 2) return false;
      var path = t.p.map(function (p) { return { lat: p[0], lng: p[1] }; });
      trail = new google.maps.Polyline({
        path: path, map: map, zIndex: 400,
        strokeColor: '#1F6F8B', strokeOpacity: 0.9, strokeWeight: 3
      });
      // Where the path starts, so the direction of travel is readable.
      trailHead = new google.maps.Marker({
        position: path[0], map: map, zIndex: 401,
        title: t.n + ' - start of the last ' + GT_TRAIL_HOURS + ' h',
        icon: {
          path: google.maps.SymbolPath.CIRCLE, scale: 5,
          fillColor: '#1F6F8B', fillOpacity: 0.95,
          strokeColor: '#FFFFFF', strokeWeight: 2
        }
      });
      trailFor = t.n;
      return true;
    }

    function draw() {
      if (markers.length || typeof map === 'undefined' || !map) return;
      if (!window.google || !window.google.maps) return;
      gtTrucks.forEach(function (t) {
        var mins = ageMinutes(t.t);
        var stopped = stoppedMinutes(t);
        var state = stateOf(t);
        var mk = new google.maps.Marker({
          position: { lat: t.y, lng: t.x },
          map: map,
          icon: truckIcon(t),
          title: t.n + '  (' + t.m + ')  -  ' + stoppedText(t) +
                 '  -  reported ' + ageText(mins),
          zIndex: 500 + (state === 'moving' ? 700 :
                   stopped === null ? 0 : Math.max(0, 600 - Math.round(stopped)))
        });
        mk.addListener('click', function () {
          // Clicking the truck whose trail is already up puts it away.
          var had = trailFor === t.n;
          var drew = false;
          if (had) clearTrail(); else drew = showTrail(t);
          var html = '<div style="font-family:inherit;font-size:13px">' +
            '<strong>' + escapeHtml(t.n) + '</strong><br>' +
            escapeHtml(t.m) + ' \u00b7 ' + escapeHtml(t.k) + '<br>' +
            '<span style="color:' + STATE_COLOUR[state] + ';font-weight:600">' +
            escapeHtml(stoppedText(t)) + '</span><br>' +
            '<span style="color:#5B6350">last reported ' + ageText(mins) +
            (state === 'idling' ? '<br>engine running - operating hours are ' +
              'still climbing' : '') +
            (state === 'stale' ? '<br>nothing reported in over ' + QUIET_DAYS +
              ' days, so this position may be out of date' : '') +
            '<br>' + (drew
              ? 'path of the last ' + GT_TRAIL_HOURS + ' h shown - click ' +
                'the truck again to hide it'
              : had ? 'path hidden'
                    : 'no movement recorded in the last ' + GT_TRAIL_HOURS + ' h') +
            '</span></div>';
          infoWindow.setContent(html);
          infoWindow.open(map, mk);
        });
        markers.push(mk);
      });
      if (summary) {
        var counts = { moving: 0, idling: 0, stopped: 0, stale: 0 };
        gtTrucks.forEach(function (t) { counts[stateOf(t)]++; });
        var bits = [gtTrucks.length + ' vehicles'];
        if (counts.moving) bits.push(counts.moving + ' moving');
        if (counts.idling) bits.push(counts.idling + ' idling');
        if (counts.stopped) bits.push(counts.stopped + ' stopped');
        if (counts.stale) bits.push(counts.stale + ' not reporting');
        var longest = null;
        gtTrucks.forEach(function (t) {
          if (stateOf(t) === 'moving') return;
          var m = stoppedMinutes(t);
          if (m !== null && (longest === null || m > longest)) longest = m;
        });
        if (longest !== null) bits.push('longest sat ' + durationText(longest));
        summary.textContent = bits.join(' \u00b7 ');
      }
    }

    var tries = 0;
    (function wait() {
      draw();
      if (!markers.length && tries++ < 60) setTimeout(wait, 500);
    })();

    // Called when the relay delivers a newer set: clear and redraw rather than
    // moving markers, since vehicles can appear and disappear between pushes.
    window.gtRedrawTrucks = function () {
      var was = trailFor;
      clearTrail();
      markers.forEach(function (m) { m.setMap(null); });
      markers.length = 0;
      draw();
      // Keep the trail the user was looking at, now with the newer points.
      if (was) {
        gtTrucks.forEach(function (t) { if (t.n === was) showTrail(t); });
      }
      if (box && !box.checked) {
        markers.forEach(function (m) { m.setMap(null); });
      }
    };

    if (box) {
      box.addEventListener('change', function () {
        markers.forEach(function (m) { m.setMap(box.checked ? map : null); });
        if (!box.checked) clearTrail();
      });
    }

    // ---- long stops -----------------------------------------------------
    // A truck standing still for more than gtStopMinutes while its tracker
    // was still reporting every half-minute. See dev/jd_trail.py for why
    // that is the test rather than "speed was zero": a parked truck's
    // tracker sleeps, and treating one long sleep as a stop turned a night
    // in the yard into a 72-hour idle.
    var stopList = container.querySelector('#gt-stop-list');
    var stopCount = container.querySelector('#gt-stop-count');

    function localTime(iso) {
      var t = Date.parse(iso);
      if (isNaN(t)) return '';
      var d = new Date(t), now = new Date();
      var hhmm = d.toLocaleTimeString(undefined,
        { hour: 'numeric', minute: '2-digit' });
      if (d.toDateString() === now.toDateString()) return hhmm;
      return d.toLocaleDateString(undefined,
        { month: 'short', day: 'numeric' }) + ' ' + hhmm;
    }

    window.gtRenderStops = function () {
      if (!stopList) return;
      var stops = (typeof gtStops === 'undefined') ? [] : gtStops;
      var threshold = (typeof gtStopMinutes === 'undefined') ? 10 : gtStopMinutes;
      stopList.innerHTML = '';
      if (stopCount) {
        stopCount.textContent = stops.length
          ? stops.length + ' over ' + threshold + ' min in 24 h'
          : 'none over ' + threshold + ' min in 24 h';
      }
      if (!stops.length) {
        var none = document.createElement('div');
        none.className = 'gt-stop-none';
        none.textContent = 'No truck has stood still for more than ' +
          threshold + ' minutes in the last 24 hours.';
        stopList.appendChild(none);
        return;
      }
      stops.forEach(function (s) {
        var row = document.createElement('div');
        row.className = 'gt-stop-item';
        var who = document.createElement('span');
        who.textContent = s.n;
        var mins = document.createElement('span');
        mins.className = 'gt-stop-mins';
        mins.textContent = durationText(s.m);
        var when = document.createElement('span');
        when.className = 'gt-stop-when';
        when.textContent = localTime(s.a);
        row.appendChild(who);
        row.appendChild(mins);
        row.appendChild(when);
        row.title = s.n + ' stood still ' + durationText(s.m) +
          ' from ' + localTime(s.a) + ' to ' + localTime(s.z);
        row.addEventListener('click', function () {
          if (typeof map === 'undefined' || !map || !s.y) return;
          map.panTo(new google.maps.LatLng(s.y, s.x));
          if (map.getZoom() < 14) map.setZoom(14);
        });
        stopList.appendChild(row);
      });
    };

    window.gtRenderStops();
  })();
"""


def main() -> None:
    outlines = "--outlines" in sys.argv
    if not FLEET.exists():
        sys.exit(f"no {FLEET} - run dev/jd_fleet.py first")
    fleet = json.loads(FLEET.read_text(encoding="utf-8"))
    fields = [f for f in fleet.get("fields", [])
              if f.get("lat") is not None and f.get("lon") is not None]
    if not fields:
        sys.exit("fleet.json has no fields with a centroid")

    html = SOURCE.read_text(encoding="ascii")
    for marker in (DATA_MARKER, SETUP_MARKER):
        if marker not in html:
            sys.exit(f"could not find an insertion point in {SOURCE}:\n  {marker!r}")

    trucks = [t for t in fleet.get("trucks", [])
              if t.get("lat") is not None and t.get("lon") is not None]

    # The relay, if configured, so the page can refresh positions on its own
    # instead of waiting to be re-pasted. The baked-in trucks stay as the
    # starting picture and as a fallback when the relay cannot be reached.
    relay = SECRETS / "relay.json"
    relay_js = "  var GT_FLEET_URL = '';\n  var GT_FLEET_TOKEN = '';\n"
    if relay.exists():
        cfg = json.loads(relay.read_text(encoding="utf-8-sig"))
        if cfg.get("url") and cfg.get("read_token"):
            relay_js = (f"  var GT_FLEET_URL = {json.dumps(cfg['url'])};\n"
                        f"  var GT_FLEET_TOKEN = {json.dumps(cfg['read_token'])};\n")

    html = html.replace(
        DATA_MARKER,
        build_field_js(fields, outlines) + build_truck_js(trucks)
        + build_stops_js(fleet.get("stops") or [],
                         fleet.get("stop_minutes") or 10)
        + f"  var GT_TRAIL_HOURS = {TRAIL_HOURS};\n"
        + relay_js + "\n" + DATA_MARKER,
        1)
    html = html.replace(SETUP_MARKER, SETUP_MARKER + "\n" + PANEL_JS, 1)
    # The panel sits above the map, and its styles go with the rest.
    html = html.replace('  <div class="gt-map-wrap">',
                        PANEL_HTML + '\n  <div class="gt-map-wrap">', 1)
    html = html.replace("  #grain-trucking-tool .gt-legend {",
                        PANEL_CSS + "\n  #grain-trucking-tool .gt-legend {", 1)

    if any(ord(c) > 127 for c in html):
        sys.exit("refusing to write: the result is not pure ASCII")

    OUTPUT.write_text(html, encoding="ascii", newline="\n")
    kb = OUTPUT.stat().st_size / 1024
    print(f"wrote {OUTPUT}  ({kb:,.0f} KB)")
    print(f"  {len(fields)} fields"
          + ("  with outlines" if outlines else "  (names, centroids, acres)"))
    print(f"  {len(trucks)} trucks with a position")
    print()
    print("  Paste this into a PASSWORD-PROTECTED Squarespace page.")
    print("  It contains your field locations - do not put it on a public page,")
    print("  and it is deliberately not written into the repo.")


if __name__ == "__main__":
    main()
