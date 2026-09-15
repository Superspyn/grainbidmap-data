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
from jd_common import signed_area  # noqa: E402

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
            # v: km/h off the newest breadcrumb, null when there is no recent
            # one. Converted to mph for display, not here, so the stored
            # value stays the unit Deere actually sends.
            "v:" + ("null" if t.get("speed_kmh") is None
                    else str(round(float(t["speed_kmh"]), 1))),
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
            # e is the engine verdict, i the minutes it actually ran. A stop
            # with no device reports covering it stays "unknown" and must
            # read that way on the map rather than defaulting to parked.
            "e:" + json.dumps(str(s.get("engine") or "unknown")),
            "i:" + ("null" if s.get("idle_min") is None
                    else str(int(s["idle_min"]))),
            f"y:{round(float(s.get('y') or 0), 6)}",
            f"x:{round(float(s.get('x') or 0), 6)}",
        ]) + "}")
    return ("  var gtStops = [\n    " + ",\n    ".join(rows) + "\n  ];\n"
            + f"  var gtStopMinutes = {int(threshold)};\n")


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
        if f.get("crop"):
            # c: what the latest planting pass put in; cs: the season it is
            # from, so the page can say when it is last year's.
            parts.append("c:" + json.dumps(str(f["crop"])))
            if f.get("crop_season"):
                parts.append(f"cs:{int(f['crop_season'])}")
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
  #grain-trucking-tool .gt-truck-filter { margin: 7px 0 0; }
  #grain-trucking-tool .gt-truck-filter select {
    width: 100%; padding: 5px 7px; font-size: 12px; font-family: inherit;
    color: var(--ink); background: #fff; border: 1px solid var(--line);
    border-radius: 5px; }
  #grain-trucking-tool .gt-truck-list {
    max-height: 168px; overflow-y: auto; margin-top: 5px; }
  #grain-trucking-tool .gt-truck-item {
    display: flex; gap: 7px; align-items: baseline; padding: 3px 7px;
    font-size: 12px; border-radius: 4px; cursor: pointer; }
  #grain-trucking-tool .gt-truck-item:hover { background: #EFF2EA; }
  #grain-trucking-tool .gt-truck-item i {
    width: 8px; height: 8px; border-radius: 50%; flex: none; }
  #grain-trucking-tool .gt-truck-item .gt-truck-name {
    flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; }
  #grain-trucking-tool .gt-truck-item .gt-truck-for {
    color: #5B6350; flex: none; }
  #grain-trucking-tool .gt-truck-empty {
    padding: 5px 7px; font-size: 12px; color: #5B6350; }
  #grain-trucking-tool .gt-stop-tag { flex: none; font-size: 10.5px;
    padding: 1px 5px; border-radius: 8px; letter-spacing: .02em; }
  #grain-trucking-tool .gt-stop-idling { background: #F6E0C0; color: #8A5A12; }
  #grain-trucking-tool .gt-stop-parked { background: #E3E7DC; color: #5B6350; }
  #grain-trucking-tool .gt-stop-unknown { background: #EDEDED; color: #777; }
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
  #grain-trucking-tool .gt-crop-key { margin-top: 6px; font-size: 11.5px; color: var(--ink-soft); }
  #grain-trucking-tool .gt-crop-note { display: block; margin-top: 2px; font-size: 11px; }
  #grain-trucking-tool .gt-crop-tag {
    flex: none; font-size: 10.5px; padding: 1px 6px; border-radius: 8px;
    letter-spacing: .02em; white-space: nowrap;
  }
  #grain-trucking-tool .gt-crop-corn { background: #F3E3C3; color: #7A5210; }
  #grain-trucking-tool .gt-crop-soybeans { background: #D7EADF; color: #245A3C; }
  #grain-trucking-tool .gt-crop-other { background: #E6E8E1; color: #5B6350; }
  #grain-trucking-tool .gt-box-split { font-size: 11.5px; color: var(--ink-soft); margin-top: 2px; }
  #grain-trucking-tool .gt-box-bushels .gt-box-line { display: block; font-size: 12px; margin-top: 1px; }
  #grain-trucking-tool .gt-box-bushels .gt-box-line b { font-size: 13px; }
  #grain-trucking-tool .gt-box-buttons { display: flex; gap: 6px; margin-top: 8px; }
  #grain-trucking-tool .gt-box-btn {
    font: inherit; font-size: 12px; padding: 6px 10px; cursor: pointer;
    border: 1px solid #3F8F3F; border-radius: 5px; color: #fff; background: #3F8F3F;
  }
  #grain-trucking-tool .gt-box-btn:hover { background: #357A35; }
  #grain-trucking-tool .gt-box-btn-quiet {
    color: var(--ink-soft); background: #fff; border-color: var(--line);
  }
  #grain-trucking-tool .gt-box-btn-quiet:hover { background: var(--bg); }
  #grain-trucking-tool .gt-box-result {
    margin-top: 8px; padding: 8px 9px; border: 1px solid var(--line);
    border-radius: 6px; background: var(--bg);
  }
  #grain-trucking-tool .gt-box-total { font-size: 13px; font-weight: 600; }
  #grain-trucking-tool .gt-box-total span {
    font-family: 'IBM Plex Mono', monospace; font-size: 16px; color: #2E6B2E;
  }
  #grain-trucking-tool .gt-box-gross { font-size: 11.5px; color: var(--ink-soft); margin-top: 2px; }
  #grain-trucking-tool .gt-box-yield {
    display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 12px;
  }
  #grain-trucking-tool .gt-box-yield input {
    width: 76px; padding: 4px 6px; font: inherit; font-size: 13px;
    font-family: 'IBM Plex Mono', monospace; border: 1px solid var(--line);
    border-radius: 4px; color: var(--ink); background: #fff;
  }
  #grain-trucking-tool .gt-box-bushels { font-size: 13px; margin-top: 6px; }
  #grain-trucking-tool .gt-box-bushels b {
    font-family: 'IBM Plex Mono', monospace; font-size: 16px; color: #2E6B2E;
  }
  #grain-trucking-tool .gt-box-bushels .gt-box-loads {
    display: block; font-size: 11.5px; color: var(--ink-soft); margin-top: 1px;
  }
  #grain-trucking-tool .gt-box-list { max-height: 150px; overflow-y: auto; margin-top: 7px; }
  #grain-trucking-tool .gt-box-item {
    display: flex; gap: 8px; align-items: baseline; padding: 2px 4px; font-size: 12px;
    cursor: pointer; border-radius: 3px;
  }
  #grain-trucking-tool .gt-box-item:hover { background: #E8ECE0; }
  #grain-trucking-tool .gt-box-item span:first-child {
    flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  #grain-trucking-tool .gt-box-part { color: var(--ink-soft); font-size: 11px; white-space: nowrap; }
  #grain-trucking-tool .gt-box-btn-on { background: #1F5E1F; border-color: #1F5E1F; color: #fff; }
  #grain-trucking-tool .gt-sel-hint {
    margin-top: 6px; padding: 5px 8px; font-size: 11.5px; color: #1F5E1F;
    background: #E6F0E6; border-radius: 5px;
  }
  #grain-trucking-tool .gt-sel-contract select {
    padding: 4px 5px; font: inherit; font-size: 12px; border: 1px solid var(--line);
    border-radius: 4px; background: #fff; color: var(--ink);
  }
  #grain-trucking-tool .gt-sel-contract input { width: 92px; }
  #grain-trucking-tool .gt-sel-contract-out {
    font-size: 12px; margin-top: 5px; line-height: 1.45; color: #2E6B2E;
  }
  #grain-trucking-tool .gt-sel-contract-out.gt-sel-short { color: #A23B2A; }
  #grain-trucking-tool .gt-box-x {
    flex: none; color: var(--ink-soft); font-size: 13px; padding: 0 3px; cursor: pointer;
  }
  #grain-trucking-tool .gt-box-x:hover { color: #A23B2A; }
  #grain-trucking-tool .gt-haul {
    margin-top: 9px; padding-top: 8px; border-top: 1px solid var(--line);
  }
  #grain-trucking-tool .gt-haul-head { font-size: 12.5px; margin-bottom: 6px; }
  #grain-trucking-tool .gt-haul-status { font-size: 11.5px; color: var(--ink-soft); margin-top: 5px; }
  #grain-trucking-tool .gt-haul-summary { margin-top: 5px; }
  #grain-trucking-tool .gt-haul-warn { color: #8A5A12; }
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
    <div class="gt-crop-key" id="gt-crop-key">
      <span class="gt-truck-key"><i style="background:#C08A28"></i>corn</span>
      <span class="gt-truck-key"><i style="background:#4F9A6E"></i>soybeans</span>
      <span class="gt-truck-key"><i style="background:#98A08C"></i>no planting pass</span>
      <span class="gt-crop-note">from this season&rsquo;s planting pass in Operations Center</span>
    </div>
    <div class="gt-field-note" id="gt-field-note">Pick a field &mdash; by name or
      by clicking its outline &mdash; to use it as your loadout point. Every haul
      cost and the best-bids table below are then calculated from there.</div>
    <div class="gt-field-head" style="margin-top:12px;padding-top:11px;border-top:1px solid var(--line);">
      <span class="gt-field-title">Select fields</span>
      <span class="gt-field-count" id="gt-box-count"></span>
    </div>
    <div class="gt-box-buttons">
      <button type="button" class="gt-box-btn gt-box-btn-quiet" id="gt-sel-pick">Pick fields on the map</button>
      <button type="button" class="gt-box-btn" id="gt-box-start">Drop a box</button>
      <button type="button" class="gt-box-btn gt-box-btn-quiet" id="gt-box-clear" hidden>Clear</button>
    </div>
    <div class="gt-sel-hint" id="gt-sel-hint" hidden>Click a field outline to add it; click it
      again to take it out. Press <em>Done picking</em> when you have them.</div>
    <div class="gt-box-result" id="gt-box-result" hidden>
      <div class="gt-box-total"><span id="gt-box-acres">0</span> acres selected</div>
      <div class="gt-box-gross" id="gt-box-gross"></div>
      <div class="gt-box-split" id="gt-box-split"></div>
      <div class="gt-box-yield">
        <label for="gt-box-yield-corn">Corn yield</label>
        <input type="number" id="gt-box-yield-corn" min="0" step="1" inputmode="decimal" placeholder="e.g. 210">
        <span>bu/ac</span>
      </div>
      <div class="gt-box-yield">
        <label for="gt-box-yield-beans">Bean yield</label>
        <input type="number" id="gt-box-yield-beans" min="0" step="1" inputmode="decimal" placeholder="e.g. 60">
        <span>bu/ac</span>
      </div>
      <div class="gt-box-bushels" id="gt-box-bushels"></div>
      <div class="gt-box-yield gt-sel-contract">
        <label for="gt-sel-contract">Contract</label>
        <select id="gt-sel-crop"><option value="corn">corn</option><option value="soybeans">beans</option></select>
        <input type="number" id="gt-sel-contract" min="0" step="1000" inputmode="numeric" placeholder="bushels">
        <span>bu</span>
      </div>
      <div class="gt-sel-contract-out" id="gt-sel-contract-out"></div>
      <div class="gt-box-list" id="gt-box-list"></div>
      <div class="gt-haul" id="gt-haul" hidden>
        <div class="gt-haul-head">Haul cost to <b id="gt-haul-dest"></b></div>
        <button type="button" class="gt-box-btn" id="gt-haul-run">Route every selected field</button>
        <div class="gt-haul-status" id="gt-haul-status"></div>
        <div class="gt-box-bushels gt-haul-summary" id="gt-haul-summary"></div>
        <div class="gt-box-list" id="gt-haul-list"></div>
      </div>
    </div>
    <div class="gt-field-note">Build a selection by clicking fields, by
      dropping a box and dragging it over the ground you want, or both. A
      clicked field counts whole; a field the box&rsquo;s edge cuts through
      counts only the part inside. The acres are split by what Operations
      Center says was planted; a corn yield and a bean yield turn them into
      bushels, and a contract size says whether the selection covers it.
      Then click an elevator pin as usual: the selection routes every field
      to it &mdash; real driving routes, one per field &mdash; and gives a haul
      cost weighted by each field&rsquo;s bushels, the total, and the net
      after the bid. A weight-restricted bridge on any route is flagged.</div>
    <div class="gt-field-head" style="margin-top:12px;padding-top:11px;border-top:1px solid var(--line);">
      <label class="gt-field-toggle" style="margin-top:0;">
        <input type="checkbox" id="gt-truck-toggle" checked>
        Show trucks on the map
      </label>
      <span class="gt-field-count" id="gt-truck-summary"></span>
    </div>
    <div class="gt-truck-filter">
      <select id="gt-truck-state">
        <option value="all">All vehicles</option>
        <option value="moving">Moving</option>
        <option value="idling">Idling &mdash; engine on, not moving</option>
        <option value="stopped">Stopped &mdash; engine off</option>
        <option value="stale">Not reporting</option>
      </select>
    </div>
    <div class="gt-truck-list" id="gt-truck-list"></div>
    <div class="gt-field-note">
      <span class="gt-truck-key"><i style="background:#3F8F3F"></i>moving</span>
      <span class="gt-truck-key"><i style="background:#D2901F"></i>idling, engine on</span>
      <span class="gt-truck-key"><i style="background:#C0392B"></i>stopped</span>
      <span class="gt-truck-key"><i style="background:#98A08C"></i>not reporting</span>
      <br>Idling and stopped are told apart by the tracker&rsquo;s battery
      voltage: an alternator holds 13.2&ndash;14.5&thinsp;V, a battery on its
      own sits near 12.6&thinsp;V. Deere&rsquo;s own engine-hours field is
      dead on these trucks, so this is measured rather than reported. Click a
      truck for how long it has sat and to draw the path it drove in the last
      24 hours. &ldquo;At least&rdquo; means the history ran out before it
      found the truck somewhere else. Positions are each vehicle&rsquo;s last
      report, roughly ten to twenty minutes behind, and a speed is only shown
      while its reading is under ten minutes old.</div>
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
    var cropTally = { corn: 0, soybeans: 0 };
    gtFields.forEach(function (f) { if (cropTally.hasOwnProperty(f.c)) cropTally[f.c]++; });
    count.textContent = gtFields.length + ' fields \u00b7 ' +
      acresTotal.toLocaleString(undefined, { maximumFractionDigits: 0 }) + ' acres' +
      ((cropTally.corn || cropTally.soybeans)
        ? ' \u00b7 ' + cropTally.corn + ' corn, ' + cropTally.soybeans + ' beans' : '');

    // Crop as a small tag, wherever a field is listed. The word comes from
    // the page data as Deere gave it; only corn and soybeans get a colour.
    function cropKey(f) {
      return f.c === 'corn' ? 'corn' : f.c === 'soybeans' ? 'soybeans' : 'other';
    }
    function cropTag(f) {
      var tag = document.createElement('span');
      tag.className = 'gt-crop-tag gt-crop-' + cropKey(f);
      tag.textContent = f.c ? (f.c === 'soybeans' ? 'beans' : f.c) +
        (f.cs && f.cs !== new Date().getFullYear() ? ' (' + f.cs + ')' : '') : 'no crop';
      return tag;
    }
    var CROP_FILL = { corn: '#C08A28', soybeans: '#4F9A6E', other: '#98A08C' };
    var CROP_LINE = { corn: '#9C6E1C', soybeans: '#2F6E4B', other: '#6E7565' };

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
        row.appendChild(cropTag(f));
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

    // What a field outline looks like: bold while it is picked, brighter
    // while a box is counting it, the usual wash otherwise. One place, so
    // the hover, the box, the picker and Clear all agree.
    function fieldStyle(f) {
      return { fillOpacity: f._picked ? 0.5 : f._inBox ? 0.42 : 0.18,
               strokeWeight: f._picked ? 3 : 1.5 };
    }
    function paint(f) {
      if (f._poly) f._poly.setOptions(fieldStyle(f));
    }
    // Clicking an outline chooses it as the haul origin - unless the
    // selection tool below is picking, in which case it reassigns this.
    var fieldClick = choose;

    // A field's rings, decoded once and kept on the field: the outlines and
    // the measuring box both need them, and there are ~150,000 vertices.
    function rings(f) {
      if (!f._rings) {
        f._rings = (f.r || []).map(function (r) {
          return { hole: r.charAt(0) === 'i', pts: decodeRing(r.slice(1)) };
        });
      }
      return f._rings;
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
          paths: rings(f).map(function (r) { return r.pts; }),
          strokeColor: CROP_LINE[cropKey(f)], strokeOpacity: 0.9, strokeWeight: 1.5,
          fillColor: CROP_FILL[cropKey(f)], fillOpacity: 0.18,
          map: map, zIndex: 1, clickable: true
        });
        poly.addListener('click', function () { fieldClick(f); });
        poly.addListener('mouseover', function () {
          poly.setOptions({ fillOpacity: Math.max(0.35, fieldStyle(f).fillOpacity) });
        });
        poly.addListener('mouseout', function () { paint(f); });
        f._poly = poly;
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

    // ---- bushels in a box ------------------------------------------------
    // A rectangle you drag over the map; the acres of your fields inside it,
    // and at a yield you type, the bushels. It works from the same boundary
    // rings the outlines are drawn from, so what is counted is exactly what
    // is on the screen.
    //
    // Each ring is clipped to the box (Sutherland-Hodgman - exact for a
    // rectangle, since the box's edges are lines of constant latitude and
    // longitude). Exterior rings add, holes subtract, and the clipped area
    // over the unclipped area is the share of the field inside the box. That
    // share times Deere's own acreage is what is reported, so a field wholly
    // inside comes out at the figure Operations Center shows for it, to the
    // cent, and only a field cut by the edge is ever pro-rated.
    // ---- select fields --------------------------------------------------
    // Two ways to build a selection - click fields one at a time, or drop a
    // box and take what is inside - and three things to do with it: total
    // the acres by crop, check them against a contract, and route every
    // field to one elevator for a bushel-weighted haul cost. A clicked
    // field is whole; a field the box's edge cuts through counts only the
    // part inside, exactly as the box did on its own.
    //
    // The haul cost reuses the page's own model - haulCosts() and
    // netPerBushel() price the single route above, and they price these -
    // so the weighted figure and the one-field figure can never disagree
    // about what a mile costs.
    (function setupSelection() {
      var pickBtn = container.querySelector('#gt-sel-pick');
      var startBtn = container.querySelector('#gt-box-start');
      var clearBtn = container.querySelector('#gt-box-clear');
      var hint = container.querySelector('#gt-sel-hint');
      var result = container.querySelector('#gt-box-result');
      var acresOut = container.querySelector('#gt-box-acres');
      var grossOut = container.querySelector('#gt-box-gross');
      var countOut = container.querySelector('#gt-box-count');
      var yieldCorn = container.querySelector('#gt-box-yield-corn');
      var yieldBeans = container.querySelector('#gt-box-yield-beans');
      var splitOut = container.querySelector('#gt-box-split');
      var bushelsOut = container.querySelector('#gt-box-bushels');
      var listOut = container.querySelector('#gt-box-list');
      var cropSel = container.querySelector('#gt-sel-crop');
      var contractIn = container.querySelector('#gt-sel-contract');
      var contractOut = container.querySelector('#gt-sel-contract-out');
      var haul = container.querySelector('#gt-haul');
      var haulDest = container.querySelector('#gt-haul-dest');
      var haulRun = container.querySelector('#gt-haul-run');
      var haulStatus = container.querySelector('#gt-haul-status');
      var haulSummary = container.querySelector('#gt-haul-summary');
      var haulList = container.querySelector('#gt-haul-list');
      if (!startBtn || !result) return;

      var SQM_PER_ACRE = 4046.8564224;
      var MIDDOT = ' ' + String.fromCharCode(183) + ' ';
      var rect = null, pending = false, lastBox = null, picking = false;
      var picked = {}, dropped = {}, boxed = {};   // field name -> true / true / {acres, share}
      var rows = [], lastBy = { corn: 0, soybeans: 0, other: 0 };
      var routed = {};                              // "field|dest" -> {miles, hours, bridges} or {error}

      // Metres per degree at a latitude: a flat local frame is fine here,
      // since the ratio is what matters for fields and the gross box figure
      // only needs to be within a fraction of a percent.
      function frame(lat) {
        var rad = Math.PI / 180;
        return { kx: 111320 * Math.cos(lat * rad), ky: 111132 };
      }

      function ringArea(pts, fr) {
        var s = 0;
        for (var i = 0, n = pts.length; i < n; i++) {
          var a = pts[i], b = pts[(i + 1) % n];
          s += (a.lng * fr.kx) * (b.lat * fr.ky) - (b.lng * fr.kx) * (a.lat * fr.ky);
        }
        return Math.abs(s) / 2;
      }

      // Clip a ring to the box, one edge at a time.
      function clipRing(pts, box) {
        var edges = [
          function (p) { return p.lat >= box.s; },
          function (p) { return p.lat <= box.n; },
          function (p) { return p.lng >= box.w; },
          function (p) { return p.lng <= box.e; }
        ];
        var lines = [
          function (p, q) { var t = (box.s - p.lat) / (q.lat - p.lat); return { lat: box.s, lng: p.lng + t * (q.lng - p.lng) }; },
          function (p, q) { var t = (box.n - p.lat) / (q.lat - p.lat); return { lat: box.n, lng: p.lng + t * (q.lng - p.lng) }; },
          function (p, q) { var t = (box.w - p.lng) / (q.lng - p.lng); return { lng: box.w, lat: p.lat + t * (q.lat - p.lat) }; },
          function (p, q) { var t = (box.e - p.lng) / (q.lng - p.lng); return { lng: box.e, lat: p.lat + t * (q.lat - p.lat) }; }
        ];
        var out = pts;
        for (var k = 0; k < 4 && out.length; k++) {
          var inside = edges[k], cross = lines[k], inp = out;
          out = [];
          for (var i = 0, n = inp.length; i < n; i++) {
            var cur = inp[i], prev = inp[(i + n - 1) % n];
            var curIn = inside(cur), prevIn = inside(prev);
            if (curIn) {
              if (!prevIn) out.push(cross(prev, cur));
              out.push(cur);
            } else if (prevIn) {
              out.push(cross(prev, cur));
            }
          }
        }
        return out;
      }

      // Each field's extent, local frame and full area, worked out once.
      // The rings themselves come from rings(f), shared with the outlines,
      // so nothing is polyline-decoded twice.
      function prepared(f) {
        if (f._ext) return f;
        var s = 90, n = -90, w = 180, e = -180;
        rings(f).forEach(function (r) {
          r.pts.forEach(function (p) {
            if (p.lat < s) s = p.lat; if (p.lat > n) n = p.lat;
            if (p.lng < w) w = p.lng; if (p.lng > e) e = p.lng;
          });
        });
        f._ext = { s: s, n: n, w: w, e: e };
        f._fr = frame(f.y);
        var full = 0;
        rings(f).forEach(function (r) { full += (r.hole ? -1 : 1) * ringArea(r.pts, f._fr); });
        f._full = full;
        return f;
      }

      // Acres of one field inside the box, or 0.
      function acresInside(f, box) {
        if (!f.r || !f.r.length) return 0;
        prepared(f);
        var x = f._ext;
        if (x.n < box.s || x.s > box.n || x.e < box.w || x.w > box.e) return 0;
        var inside = 0;
        rings(f).forEach(function (r) {
          var c = clipRing(r.pts, box);
          if (c.length > 2) inside += (r.hole ? -1 : 1) * ringArea(c, f._fr);
        });
        if (inside <= 0 || f._full <= 0) return 0;
        // Deere's figure when it is there; the boundary's own area if not.
        var acres = f.a ? f.a * Math.min(1, inside / f._full) : inside / SQM_PER_ACRE;
        return acres < 0.05 ? 0 : acres;
      }

      function wholeAcres(f) {
        if (f.a) return f.a;
        return (f.r && f.r.length) ? prepared(f)._full / SQM_PER_ACRE : 0;
      }

      function yields() {
        return { corn: parseFloat(yieldCorn.value) || 0,
                 soybeans: parseFloat(yieldBeans.value) || 0, other: 0 };
      }

      function fmt(n, d) {
        return n.toLocaleString(undefined, { maximumFractionDigits: d === undefined ? 0 : d,
                                             minimumFractionDigits: d === undefined ? 0 : d });
      }

      // ---- the selection itself ----
      // A picked field is whole. A boxed field is its share. A field the
      // user crossed off stays off until Clear, even if the box still
      // covers it.
      function selection() {
        var out = [];
        gtFields.forEach(function (f) {
          if (dropped[f.n]) return;
          var b = boxed[f.n];
          if (picked[f.n]) out.push({ f: f, acres: wholeAcres(f), share: 1, how: 'picked' });
          else if (b) out.push({ f: f, acres: b.acres, share: b.share, how: 'box' });
        });
        out.sort(function (a, c) { return c.acres - a.acres; });
        return out;
      }

      function togglePick(f) {
        if (picked[f.n]) {
          delete picked[f.n];
          if (boxed[f.n]) dropped[f.n] = true;
        } else if (boxed[f.n] && !dropped[f.n]) {
          dropped[f.n] = true;                // a boxed field, clicked: take it out
        } else {
          picked[f.n] = true;
          delete dropped[f.n];
        }
        recompute();
      }

      function recompute() {
        rows = selection();
        var on = {};
        rows.forEach(function (r) { on[r.f.n] = r; });
        gtFields.forEach(function (f) {
          var r = on[f.n];
          var pick = !!(r && r.how === 'picked'), box = !!(r && r.how === 'box');
          if (pick !== !!f._picked || box !== !!f._inBox) {
            f._picked = pick; f._inBox = box; paint(f);
          }
        });
        var total = 0, byCrop = { corn: 0, soybeans: 0, other: 0 };
        rows.forEach(function (r) { total += r.acres; byCrop[cropKey(r.f)] += r.acres; });
        lastBy = byCrop;

        var any = rows.length || rect || picking;
        result.hidden = !any;
        clearBtn.hidden = !any;
        acresOut.textContent = fmt(total, 1);
        countOut.textContent = rows.length ? rows.length + ' fields' : '';
        var bits = [];
        if (byCrop.corn) bits.push('corn ' + byCrop.corn.toFixed(1));
        if (byCrop.soybeans) bits.push('beans ' + byCrop.soybeans.toFixed(1));
        if (byCrop.other) bits.push('no planting pass ' + byCrop.other.toFixed(1));
        splitOut.textContent = bits.length > 1 || byCrop.other ? bits.join(MIDDOT) + ' ac' : '';

        listOut.innerHTML = '';
        rows.forEach(function (r) {
          var row = document.createElement('div');
          row.className = 'gt-box-item';
          var name = document.createElement('span');
          name.textContent = r.f.n;
          row.appendChild(name);
          row.appendChild(cropTag(r.f));
          var part = document.createElement('span');
          part.className = 'gt-box-part';
          part.textContent = r.how === 'picked' ? 'picked'
            : (r.share < 0.995 && r.f.a ? Math.round(r.share * 100) + '% of ' + r.f.a.toFixed(1) : 'in box');
          var ac = document.createElement('span');
          ac.className = 'gt-field-acres';
          ac.textContent = r.acres.toFixed(1) + ' ac';
          var x = document.createElement('span');
          x.className = 'gt-box-x';
          x.textContent = String.fromCharCode(215);
          x.title = 'Take this field out of the selection';
          x.addEventListener('click', function (e) {
            e.stopPropagation();
            delete picked[r.f.n];
            if (boxed[r.f.n]) dropped[r.f.n] = true;
            recompute();
          });
          row.appendChild(part);
          row.appendChild(ac);
          row.appendChild(x);
          row.addEventListener('click', function () {
            if (typeof map !== 'undefined' && map) map.panTo(new google.maps.LatLng(r.f.y, r.f.x));
          });
          listOut.appendChild(row);
        });
        bushels();
        contract();
        renderHaul();
      }

      // ---- the box ----
      function measure() {
        pending = false;
        if (!rect) return;
        var b = rect.getBounds();
        if (!b) return;
        var box = { s: b.getSouthWest().lat(), n: b.getNorthEast().lat(),
                    w: b.getSouthWest().lng(), e: b.getNorthEast().lng() };
        // bounds_changed also fires when nothing changed; a 444-field pass
        // and a DOM rebuild are not owed for that.
        if (lastBox && lastBox.s === box.s && lastBox.n === box.n &&
            lastBox.w === box.w && lastBox.e === box.e) return;
        lastBox = box;
        boxed = {};
        gtFields.forEach(function (f) {
          var acres = acresInside(f, box);
          if (acres > 0) boxed[f.n] = { acres: acres, share: f.a ? acres / f.a : 1 };
        });
        var gross = ringArea([
          { lat: box.s, lng: box.w }, { lat: box.s, lng: box.e },
          { lat: box.n, lng: box.e }, { lat: box.n, lng: box.w }
        ], frame((box.s + box.n) / 2)) / SQM_PER_ACRE;
        var n = Object.keys(boxed).length;
        grossOut.textContent = 'The box covers ' + fmt(gross) + ' acres of ground; ' +
          n + (n === 1 ? ' field' : ' fields') + ' of yours ' + (n === 1 ? 'is' : 'are') + ' in it.';
        recompute();
      }

      function schedule() {
        // bounds_changed fires for every pixel of a drag; one pass per
        // frame is plenty.
        if (pending) return;
        pending = true;
        (window.requestAnimationFrame || setTimeout)(measure);
      }

      function mapReady() {
        return !!(window.google && window.google.maps && typeof map !== 'undefined' && map);
      }

      function dropBox() {
        if (!mapReady()) {
          grossOut.textContent = 'The map is still loading - try again in a moment.';
          result.hidden = false;
          return;
        }
        var v = map.getBounds();
        if (!v) return;
        // A box through the middle of whatever is on screen, sized so the
        // handles are easy to grab, and re-centred on every press so the
        // button also brings a box back from wherever it was left.
        var sw = v.getSouthWest(), ne = v.getNorthEast();
        var dLat = (ne.lat() - sw.lat()) * 0.22, dLng = (ne.lng() - sw.lng()) * 0.22;
        var c = map.getCenter();
        var bounds = new google.maps.LatLngBounds(
          { lat: c.lat() - dLat, lng: c.lng() - dLng },
          { lat: c.lat() + dLat, lng: c.lng() + dLng });
        if (!rect) {
          rect = new google.maps.Rectangle({
            bounds: bounds, map: map, editable: true, draggable: true,
            strokeColor: '#1F5E1F', strokeOpacity: 0.95, strokeWeight: 2,
            fillColor: '#3F8F3F', fillOpacity: 0.08, zIndex: 50
          });
          rect.addListener('bounds_changed', schedule);
        } else {
          rect.setBounds(bounds);
        }
        startBtn.textContent = 'Re-centre the box';
        lastBox = null;
        measure();
      }

      // ---- picking ----
      function setPicking(on) {
        picking = on;
        pickBtn.textContent = on ? 'Done picking' : 'Pick fields on the map';
        pickBtn.classList.toggle('gt-box-btn-on', on);
        hint.hidden = !on;
        if (mapReady()) map.setOptions({ draggableCursor: on ? 'crosshair' : null });
        recompute();
      }

      // The outline click goes to the selection while picking, and to the
      // haul-origin picker otherwise - the same shape does both jobs.
      fieldClick = function (f) {
        if (picking) togglePick(f); else choose(f);
      };

      function clearAll() {
        if (rect) { rect.setMap(null); rect = null; }
        picked = {}; dropped = {}; boxed = {};
        lastBox = null;
        startBtn.textContent = 'Drop a box';
        grossOut.textContent = '';
        if (picking) setPicking(false); else recompute();
      }

      // ---- bushels and the contract ----
      function bushels() {
        // One yield per crop. Acres with no planting pass are listed but
        // never turned into bushels - there is nothing honest to multiply
        // them by.
        var y = yields();
        var buCorn = lastBy.corn * y.corn, buBeans = lastBy.soybeans * y.soybeans;
        var bu = buCorn + buBeans;
        if (!bu) { bushelsOut.innerHTML = ''; return; }
        var lines = [];
        if (buCorn) lines.push('<span class="gt-box-line"><b>' + fmt(buCorn) +
          '</b> bu corn at ' + fmt(y.corn) + '</span>');
        if (buBeans) lines.push('<span class="gt-box-line"><b>' + fmt(buBeans) +
          '</b> bu beans at ' + fmt(y.soybeans) + '</span>');
        var html = lines.join('');
        if (buCorn && buBeans) {
          html += '<span class="gt-box-line"><b>' + fmt(bu) + '</b> bushels together</span>';
        }
        var cap = parseFloat(capInput.value);
        if (cap > 0) {
          html += '<span class="gt-box-loads">about ' + fmt(bu / cap, 1) +
            ' loads at ' + fmt(cap) + ' bu a truck</span>';
        }
        bushelsOut.innerHTML = html;
        try {
          if (y.corn > 0) localStorage.setItem('gt-box-yield-corn', String(y.corn));
          if (y.soybeans > 0) localStorage.setItem('gt-box-yield-beans', String(y.soybeans));
        } catch (e) {}
      }

      // Does the selection cover the contract? Production is the selected
      // acres of that crop at the yield typed above; the shortfall or the
      // surplus is said in bushels and in acres at that yield, which is
      // the form the next decision takes - which field to add or leave.
      function contract() {
        var crop = cropSel.value, word = crop === 'soybeans' ? 'beans' : 'corn';
        var c = parseFloat(contractIn.value);
        if (!(c > 0)) { contractOut.textContent = ''; return; }
        var y = yields()[crop], acres = lastBy[crop];
        if (!(y > 0)) {
          contractOut.textContent = 'Enter a ' + word + ' yield above to check the contract.';
          return;
        }
        var prod = acres * y, need = c / y, diff = prod - c;
        var text = 'Selected ' + word + ': ' + fmt(acres, 1) + ' ac makes ' + fmt(prod) +
          ' bu at ' + fmt(y) + '. ';
        if (!acres) {
          text = 'No ' + word + ' selected. ' + fmt(c) + ' bu at ' + fmt(y) + ' needs about ' +
            fmt(need, 1) + ' acres.';
        } else if (diff >= 0) {
          text += 'Covers the ' + fmt(c) + ' bu contract with ' + fmt(diff) + ' bu to spare - ' +
            fmt(acres - need, 1) + ' ac more than it needs.';
        } else {
          text += 'Short of the ' + fmt(c) + ' bu contract by ' + fmt(-diff) + ' bu - about ' +
            fmt(need - acres, 1) + ' more acres at ' + fmt(y) + '.';
        }
        contractOut.textContent = text;
        contractOut.classList.toggle('gt-sel-short', diff < 0 && acres > 0);
        // The page's own contract-size box feeds fixed cost per bushel;
        // a contract typed here is that contract.
        if (contractbuInput && parseFloat(contractbuInput.value) !== c) {
          contractbuInput.value = String(c);
          contractbuInput.dispatchEvent(new Event('input'));
        }
        try {
          localStorage.setItem('gt-sel-contract', String(c));
          localStorage.setItem('gt-sel-crop', crop);
        } catch (e) {}
      }

      // ---- the weighted haul ----
      function facility() {
        return (typeof selectedDestination !== 'undefined' && selectedDestination) ? selectedDestination : null;
      }

      function destKey(d) {
        var p = d.getPosition();
        return p.lat().toFixed(5) + ',' + p.lng().toFixed(5);
      }

      function weightOf(r, y) {
        // Bushels at the typed yield; failing that, acres, and the summary
        // says so.
        var perAc = y[cropKey(r.f)];
        return perAc > 0 ? r.acres * perAc : null;
      }

      var running = false;

      function routeAll() {
        var d = facility();
        if (!d || running || !mapReady() || typeof directionsService === 'undefined' || !directionsService) return;
        var key = destKey(d);
        var todo = rows.filter(function (r) { return !routed[r.f.n + '|' + key]; });
        if (!todo.length) { renderHaul(); return; }
        running = true;
        haulRun.disabled = true;
        var i = 0, retries = 0;
        function next() {
          if (i >= todo.length) {
            running = false;
            haulRun.disabled = false;
            haulStatus.textContent = '';
            renderHaul();
            return;
          }
          var r = todo[i];
          haulStatus.textContent = 'Routing ' + (i + 1) + ' of ' + todo.length + ' - ' + r.f.n;
          directionsService.route({
            origin: { lat: r.f.y, lng: r.f.x },
            destination: d.getPosition(),
            travelMode: google.maps.TravelMode.DRIVING
          }, function (res, status) {
            if (status === 'OK') {
              var leg = res.routes[0].legs[0];
              var bridges = 0;
              if (bridgeToggle.checked) {
                var path = [];
                leg.steps.forEach(function (step) {
                  step.path.forEach(function (p) { path.push({ lat: p.lat(), lng: p.lng() }); });
                });
                bridges = findNearbyBridges(path);
              }
              routed[r.f.n + '|' + key] = { miles: leg.distance.value / 1609.34,
                                           hours: (leg.duration.value * 2) / 3600,
                                           bridges: bridges };
              retries = 0;
              i++;
            } else if (status === 'OVER_QUERY_LIMIT' && retries < 3) {
              // Google's per-second limit; back off and ask again.
              retries++;
              setTimeout(next, 1200 * retries);
              return;
            } else {
              routed[r.f.n + '|' + key] = { error: status };
              retries = 0;
              i++;
            }
            renderHaul();
            setTimeout(next, 180);
          });
        }
        next();
      }

      function renderHaul() {
        var d = facility();
        if (!d || !rows.length) { haul.hidden = true; return; }
        haul.hidden = false;
        haulDest.textContent = d.gtData ? d.gtData.name : 'the selected elevator';
        var key = destKey(d), y = yields();
        var bid = parseFloat(bidInput.value);
        var sumW = 0, sumCost = 0, sumNet = 0, sumBu = 0, done = 0, missing = 0, failed = 0;
        var byAcres = false, bridged = 0;
        var lines = [];
        rows.forEach(function (r) {
          var res = routed[r.f.n + '|' + key];
          var w = weightOf(r, y);
          if (w === null) { w = r.acres; byAcres = true; }
          var bu = weightOf(r, y);
          var line = { r: r, res: res, w: w, bu: bu, cost: null, net: null };
          if (!res) missing++;
          else if (res.error) failed++;
          else {
            var c = haulCosts(res.miles, res.hours);
            line.cost = c.perBushel;
            line.net = netPerBushel(c, bid);
            if (c.perBushel !== null) {
              done++;
              sumW += w;
              sumCost += c.perBushel * w;
              if (line.net !== null) sumNet += line.net * w;
              if (bu) sumBu += bu;
            }
            if (res.bridges) bridged++;
          }
          lines.push(line);
        });

        var todo = missing;
        haulRun.textContent = todo ? 'Route ' + todo + (todo === 1 ? ' field' : ' fields') +
          (done ? ' more' : '') : 'Routed';
        haulRun.disabled = running || !todo;

        var html = '';
        if (done) {
          var avg = sumCost / sumW;
          html += '<span class="gt-box-line"><b>$' + avg.toFixed(3) + '</b> / bu weighted haul cost across ' +
            done + (done === 1 ? ' field' : ' fields') +
            (byAcres ? ', weighted by acres - type yields to weight by bushels' : ', weighted by bushels') +
            '</span>';
          if (sumBu) {
            html += '<span class="gt-box-line"><b>$' + fmt(avg * sumBu) + '</b> to haul all ' +
              fmt(sumBu) + ' bu</span>';
          }
          var c = parseFloat(contractIn.value);
          if (c > 0) {
            // The contract's own crop, weighted over its own fields.
            var crop = cropSel.value, cw = 0, cc = 0;
            lines.forEach(function (l) {
              if (l.cost !== null && cropKey(l.r.f) === crop && l.w) { cw += l.w; cc += l.cost * l.w; }
            });
            if (cw) {
              html += '<span class="gt-box-line"><b>$' + fmt((cc / cw) * c) + '</b> to haul the ' +
                fmt(c) + ' bu ' + (crop === 'soybeans' ? 'bean' : 'corn') + ' contract at $' +
                (cc / cw).toFixed(3) + ' / bu</span>';
            }
          }
          if (!isNaN(bid) && sumW) {
            html += '<span class="gt-box-line"><b>$' + (sumNet / sumW).toFixed(3) +
              '</b> / bu net at a $' + bid.toFixed(2) + ' bid, after hauling</span>';
          }
          if (bridged) {
            html += '<span class="gt-box-line gt-haul-warn">' + String.fromCharCode(9888) + ' ' + bridged +
              (bridged === 1 ? ' route crosses' : ' routes cross') + ' a weight-restricted bridge</span>';
          }
        }
        if (failed) html += '<span class="gt-box-loads">' + failed + ' could not be routed</span>';
        haulSummary.innerHTML = html;

        haulList.innerHTML = '';
        lines.forEach(function (l) {
          var row = document.createElement('div');
          row.className = 'gt-box-item';
          var name = document.createElement('span');
          name.textContent = l.r.f.n;
          row.appendChild(name);
          row.appendChild(cropTag(l.r.f));
          var mi = document.createElement('span');
          mi.className = 'gt-box-part';
          mi.textContent = !l.res ? 'not routed' : l.res.error ? 'no route'
            : l.res.miles.toFixed(1) + ' mi' + (l.res.bridges ? ' ' + String.fromCharCode(9888) : '');
          var cost = document.createElement('span');
          cost.className = 'gt-field-acres';
          cost.textContent = l.cost === null ? '' : '$' + l.cost.toFixed(3);
          row.appendChild(mi);
          row.appendChild(cost);
          haulList.appendChild(row);
        });
      }

      // The moment the page finishes routing a single pin - which is how an
      // elevator becomes selectedDestination - the weighted section wakes.
      finishRoute = (function (orig) {
        return function () {
          orig.apply(null, arguments);
          renderHaul();
        };
      })(finishRoute);
      if (typeof resetBtn !== 'undefined' && resetBtn) {
        resetBtn.addEventListener('click', function () { haul.hidden = true; });
      }
      [bidInput, shrinkInput, capInput, dieselInput, mpgInput, laborInput,
       weartearInput, insuranceInput, licenseInput].forEach(function (el) {
        if (el) el.addEventListener('input', renderHaul);
      });
      if (bridgeToggle) bridgeToggle.addEventListener('change', function () {
        routed = {};                // the warning is part of the result
        renderHaul();
      });

      try {
        var sc = localStorage.getItem('gt-box-yield-corn');
        var sb = localStorage.getItem('gt-box-yield-beans');
        var cv = localStorage.getItem('gt-sel-contract');
        var cr = localStorage.getItem('gt-sel-crop');
        if (sc && parseFloat(sc) > 0) yieldCorn.value = sc;
        if (sb && parseFloat(sb) > 0) yieldBeans.value = sb;
        if (cv && parseFloat(cv) > 0) contractIn.value = cv;
        if (cr === 'corn' || cr === 'soybeans') cropSel.value = cr;
      } catch (e) {}

      pickBtn.addEventListener('click', function () { setPicking(!picking); });
      startBtn.addEventListener('click', dropBox);
      clearBtn.addEventListener('click', clearAll);
      haulRun.addEventListener('click', routeAll);
      yieldCorn.addEventListener('input', function () { bushels(); contract(); renderHaul(); });
      yieldBeans.addEventListener('input', function () { bushels(); contract(); renderHaul(); });
      cropSel.addEventListener('change', function () { contract(); renderHaul(); });
      contractIn.addEventListener('input', function () { contract(); renderHaul(); });
      if (capInput) capInput.addEventListener('input', bushels);
    })();
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
          if (!data) return;
          // The relay carries the same shape the generator bakes in, so the
          // drawing code below does not care which one it got. An empty
          // truck list is the Worker saying its store is blank, not that
          // the fleet vanished, so the baked trucks stay; the stops are
          // updated regardless, because they are a separate window.
          if (data.trucks && data.trucks.length) {
            gtTrucks = data.trucks.map(function (t) {
              return { n: t.name, m: t.make, k: t.kind, y: t.lat, x: t.lon,
                       t: t.at, s: t.since, q: t.since_min ? 1 : 0,
                       g: t.moving ? 1 : 0, e: t.engine_on ? 1 : 0,
                       v: (typeof t.speed_kmh === 'number') ? t.speed_kmh : null,
                       p: t.trail || [] };
            });
          }
          if (data.stops && data.stops.map) {
            // e and i are the engine verdict and engine-on minutes. They
            // were missed here once, and every stop on the page read
            // "engine unknown" five seconds after it loaded.
            gtStops = data.stops.map(function (s) {
              return { n: s.name, k: s.kind, a: s.start, z: s.end,
                       m: s.minutes, e: s.engine || 'unknown',
                       i: (typeof s.idle_min === 'number') ? s.idle_min : null,
                       y: s.y, x: s.x };
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
    // Not `|| !gtTrucks.length`: this block is where the relay's redraw
    // hooks are defined, so a page built on a day the feed had no positions
    // would never draw the trucks the relay delivered five seconds later.
    if (typeof gtTrucks === 'undefined') return;
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

    // Deere sends km/h (the unit is on the field: km1hr-1). Shown in mph,
    // since that is what the speedometer in the truck says.
    function mph(kmh) {
      return Math.round(kmh / 1.609344);
    }

    function speedText(t) {
      if (typeof t.v !== 'number') return null;
      // Below a walking pace the reading is GPS noise, not motion - these
      // trackers report 2.4 km/h on a truck that has not moved in twenty
      // minutes - so it is not dressed up as a speed.
      if (t.v < 3) return null;
      return mph(t.v) + ' mph';
    }

    function stoppedText(t) {
      var state = stateOf(t);
      if (state === 'moving') {
        var sp = speedText(t);
        return sp ? 'moving, ' + sp : 'moving';
      }
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

    // One marker per truck, kept across refreshes and moved rather than
    // rebuilt. The relay delivers every five minutes, and building 37
    // markers each time - an SVG data URL apiece - was ~10,000 marker
    // constructions over a working day, with a visible blink and a closed
    // info window at each one. The icon only varies by kind and state, so
    // there are eight of them, built once.
    var byName = {}, iconCache = {};

    function iconFor(t) {
      var key = (t.k === 'pickup' ? 'pickup' : 'semi') + '|' + stateOf(t);
      if (!iconCache[key]) iconCache[key] = truckIcon(t);
      return iconCache[key];
    }

    function openInfo(mk) {
      var t = mk.gtTruck, state = mk.gtState, mins = ageMinutes(t.t);
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
        (state === 'idling' ? '<br>engine running - the tracker reads ' +
          'alternator voltage' : '') +
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
    }

    function draw() {
      // These two are plain DOM and owe nothing to Google Maps, so they are
      // built before the early returns: if the Maps script is slow or
      // blocked, you can still read which trucks are idling.
      renderTruckList();
      renderSummary();
      if (typeof map === 'undefined' || !map) return;
      if (!window.google || !window.google.maps) return;
      var live = {};
      gtTrucks.forEach(function (t) {
        var mins = ageMinutes(t.t);
        var stopped = stoppedMinutes(t);
        var state = stateOf(t);
        var title = t.n + '  (' + t.m + ')  -  ' + stoppedText(t) +
                    '  -  reported ' + ageText(mins);
        var z = 500 + (state === 'moving' ? 700 :
                 stopped === null ? 0 : Math.max(0, 600 - Math.round(stopped)));
        var mk = byName[t.n];
        if (!mk) {
          mk = new google.maps.Marker({
            position: { lat: t.y, lng: t.x },
            map: null,                  // applyVisibility() decides, below
            icon: iconFor(t), title: title, zIndex: z
          });
          mk.addListener('click', function () { openInfo(mk); });
          byName[t.n] = mk;
          markers.push(mk);
        } else {
          // Only what changed: setIcon on an unchanged icon still repaints.
          if (mk.gtState !== state || mk.gtKind !== t.k) mk.setIcon(iconFor(t));
          mk.setPosition({ lat: t.y, lng: t.x });
          mk.setTitle(title);
          mk.setZIndex(z);
        }
        mk.gtTruck = t;
        mk.gtState = state;
        mk.gtKind = t.k;
        live[t.n] = true;
      });
      // Trucks that left the feed.
      for (var i = markers.length - 1; i >= 0; i--) {
        var gone = markers[i];
        if (live[gone.gtTruck.n]) continue;
        gone.setMap(null);
        delete byName[gone.gtTruck.n];
        markers.splice(i, 1);
      }
      applyVisibility();
    }

    function renderSummary() {
      if (!summary) return;
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

    // ---- the state list -------------------------------------------------
    // The dropdown answers "which ones are idling right now" without hunting
    // across the map for amber trucks. Picking a state also hides the others
    // from the map, so the two always agree about what is being looked at.
    var stateSelect = container.querySelector('#gt-truck-state');
    var truckList = container.querySelector('#gt-truck-list');

    function chosenState() {
      return stateSelect ? stateSelect.value : 'all';
    }

    // The one rule for whether a marker is on the map: the trucks toggle,
    // then the dropdown. It was written four times in four handlers, and
    // the copy in draw() had forgotten the toggle.
    function applyVisibility() {
      var want = chosenState(), on = !box || box.checked;
      markers.forEach(function (m) {
        m.setMap(on && (want === 'all' || m.gtState === want) ? map : null);
      });
    }

    var STATE_WORD = { moving: 'moving', idling: 'idling',
                       stopped: 'stopped', stale: 'not reporting' };

    function renderTruckList() {
      if (!truckList) return;
      var want = chosenState();
      var rows = gtTrucks.filter(function (t) {
        return want === 'all' || stateOf(t) === want;
      });
      // Moving first, then the one that has been sitting the shortest: the
      // truck that just pulled up is the one worth seeing at the top.
      rows.sort(function (a, b) {
        var sa = stateOf(a), sb = stateOf(b);
        if (sa !== sb) {
          var order = { moving: 0, idling: 1, stopped: 2, stale: 3 };
          return order[sa] - order[sb];
        }
        var ma = stoppedMinutes(a), mb = stoppedMinutes(b);
        if (ma === null) return 1;
        if (mb === null) return -1;
        return ma - mb;
      });
      truckList.innerHTML = '';
      if (!rows.length) {
        var none = document.createElement('div');
        none.className = 'gt-truck-empty';
        none.textContent = want === 'all' ? 'No vehicles reporting.'
          : 'No vehicle is ' + (STATE_WORD[want] || want) + ' right now.';
        truckList.appendChild(none);
        return;
      }
      rows.forEach(function (t) {
        var state = stateOf(t);
        var row = document.createElement('div');
        row.className = 'gt-truck-item';
        var dot = document.createElement('i');
        dot.style.background = STATE_COLOUR[state];
        var name = document.createElement('span');
        name.className = 'gt-truck-name';
        name.textContent = t.n;
        var how = document.createElement('span');
        how.className = 'gt-truck-for';
        var mins = stoppedMinutes(t);
        how.textContent = state === 'moving'
          ? (speedText(t) || 'moving')
          : (mins === null ? STATE_WORD[state]
             : (t.q ? 'at least ' : '') + durationText(mins));
        row.appendChild(dot);
        row.appendChild(name);
        row.appendChild(how);
        row.title = t.n + ' \u00b7 ' + stoppedText(t);
        row.addEventListener('click', function () {
          if (typeof map === 'undefined' || !map || !t.y) return;
          map.panTo(new google.maps.LatLng(t.y, t.x));
          if (map.getZoom() < 13) map.setZoom(13);
          showTrail(t);
        });
        truckList.appendChild(row);
      });
    }

    if (stateSelect) {
      stateSelect.addEventListener('change', function () {
        renderTruckList();
        applyVisibility();            // the map shows exactly what the list shows
      });
    }

    // The panel is built once, now; the markers when Google's script lands.
    // This used to rebuild the panel on every half-second poll while the
    // map loaded, up to sixty times.
    var tries = 0;
    (function wait() {
      var ready = !!(window.google && window.google.maps &&
                     typeof map !== 'undefined' && map);
      if (ready || tries === 0) draw();
      if (!ready && tries++ < 60) setTimeout(wait, 500);
    })();

    // Called when the relay delivers a newer set. draw() moves the markers
    // that are still here, adds the new and removes the gone; the trail
    // the user was looking at is redrawn with the newer points.
    window.gtRedrawTrucks = function () {
      var was = trailFor;
      draw();
      if (was) {
        clearTrail();
        gtTrucks.forEach(function (t) { if (t.n === was) showTrail(t); });
      }
    };

    if (box) {
      box.addEventListener('change', function () {
        applyVisibility();            // back on means the dropdown's pick, not all
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
        // Idling is the one that costs fuel, so it leads with the minutes
        // the engine actually ran, not the length of the stop.
        mins.textContent = (s.e === 'idling')
          ? durationText(s.i) + ' idling' : durationText(s.m);
        var tag = document.createElement('span');
        tag.className = 'gt-stop-tag gt-stop-' + (s.e || 'unknown');
        tag.textContent = (s.e === 'idling') ? 'engine on'
          : (s.e === 'parked') ? 'parked' : 'engine unknown';
        var when = document.createElement('span');
        when.className = 'gt-stop-when';
        when.textContent = localTime(s.a);
        row.appendChild(who);
        row.appendChild(mins);
        row.appendChild(tag);
        row.appendChild(when);
        row.title = s.n + ' stood still ' + durationText(s.m) +
          ' from ' + localTime(s.a) + ' to ' + localTime(s.z) +
          (s.e === 'idling' ? ', engine running for ' + durationText(s.i)
           : s.e === 'parked' ? ', engine off' : ', engine state not reported');
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
