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
--outlines to include the thinned boundary shapes as well, which is nicer to
look at and roughly a hundred times larger.
"""
from __future__ import annotations

import json
import pathlib
import sys

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


def build_field_js(fields: list[dict], outlines: bool) -> str:
    rows = []
    for f in fields:
        if f.get("lat") is None or f.get("lon") is None:
            continue
        name = json.dumps(str(f.get("name") or ""))
        acres = f.get("acres")
        parts = [f"n:{name}", f"y:{round(f['lat'], 6)}", f"x:{round(f['lon'], 6)}"]
        if acres:
            parts.append(f"a:{acres}")
        if outlines and f.get("rings"):
            # Round to five decimals: about a metre, far finer than a field
            # outline on a hauling map needs.
            rings = [[[round(p[0], 5), round(p[1], 5)] for p in ring]
                     for ring in f["rings"]]
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
        (f.a ? ' (' + f.a + ' acres)' : '') +
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
        acres.textContent = f.a ? f.a + ' ac' : '';
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
    var drawn = [];
    function drawOutlines() {
      if (drawn.length || typeof map === 'undefined' || !map) return;
      if (!window.google || !window.google.maps) return;
      gtFields.forEach(function (f) {
        if (!f.r || !f.r.length) return;
        f.r.forEach(function (ring) {
          var poly = new google.maps.Polygon({
            paths: ring.map(function (p) { return { lat: p[0], lng: p[1] }; }),
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

    html = html.replace(
        DATA_MARKER, build_field_js(fields, outlines) + "\n" + DATA_MARKER, 1)
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
    print()
    print("  Paste this into a PASSWORD-PROTECTED Squarespace page.")
    print("  It contains your field locations - do not put it on a public page,")
    print("  and it is deliberately not written into the repo.")


if __name__ == "__main__":
    main()
