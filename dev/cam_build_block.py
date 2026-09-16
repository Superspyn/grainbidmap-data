"""Build the elevator-camera block for a PASSWORD-PROTECTED Squarespace page.

    python dev/cam_build_block.py            # writes ~/.grain-map-secrets/camera-block.html

Paste the file's contents into a Code block. It shows, for every camera in
dev/config/cameras.json, how many trucks are in line and the estimated
wait, refreshed every minute from the private relay's /cameras key that
dev/cam_watch.py keeps up to date. It is independent of the map: nothing
in it needs Google, and it can sit on its own page or above the map.

Private, not because a line count is a secret, but because the relay's
read token is baked in, and that token also reads the truck positions.
The wait's "from your own trucks' visits" line says where the farm hauls,
too. So: the same password-protected page the private map is on, or
another one - never the public map page.

Pure ASCII, like everything pasted into Squarespace, because the paste
path has mangled anything else.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, read_json  # noqa: E402

OUTPUT = SECRETS / "camera-block.html"
RELAY = SECRETS / "relay.json"

BLOCK = r"""<!-- Elevator line cameras: private page only (the read token is inside). -->
<div id="gt-cams" class="gt-cams">
  <div class="gt-cams-head">
    <span class="gt-cams-title">Elevator lines</span>
    <span class="gt-cams-asof" id="gt-cams-asof">loading&hellip;</span>
  </div>
  <div id="gt-cams-list"></div>
  <div class="gt-cams-foot">
    Counted from each elevator's public line camera every two minutes.
    The wait is trucks in line times a measured minutes-per-truck; it is an
    estimate, not a promise. Tap a name to see the camera itself.
  </div>
</div>
<style>
  .gt-cams { font: 15px/1.4 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
             color: #1c2a1e; max-width: 640px; margin: 0 auto; }
  .gt-cams-head { display: flex; justify-content: space-between; align-items: baseline;
                  border-bottom: 2px solid #2e6b3a; padding-bottom: 6px; margin-bottom: 10px; }
  .gt-cams-title { font-size: 20px; font-weight: 700; }
  .gt-cams-asof { font-size: 13px; color: #5b6b5e; }
  .gt-cams-asof.gt-cams-stale { color: #b3261e; font-weight: 600; }
  .gt-cam { border: 1px solid #d5ddd6; border-radius: 10px; padding: 12px 14px; margin-bottom: 10px;
            background: #fafcfa; }
  .gt-cam-name { font-size: 17px; font-weight: 700; color: #1c2a1e; text-decoration: none; }
  .gt-cam-name:hover { text-decoration: underline; }
  .gt-cam-tag { font-size: 12px; padding: 1px 7px; border-radius: 9px; margin-left: 8px;
                vertical-align: middle; background: #e3ede4; color: #2e6b3a; }
  .gt-cam-tag.gt-cam-closed { background: #eeeeee; color: #666; }
  .gt-cam-wait { font-size: 30px; font-weight: 800; margin: 6px 0 2px; }
  .gt-cam-wait.gt-w0 { color: #2e6b3a; }
  .gt-cam-wait.gt-w1 { color: #b7791f; }
  .gt-cam-wait.gt-w2 { color: #b3261e; }
  .gt-cam-line { font-size: 15px; }
  .gt-cam-pace { font-size: 13px; color: #5b6b5e; margin-top: 4px; }
  .gt-cam-bars { display: flex; align-items: flex-end; gap: 2px; height: 34px; margin-top: 8px; }
  .gt-cam-bar { flex: 1 1 0; min-width: 2px; background: #9dbfa4; border-radius: 2px 2px 0 0; }
  .gt-cam-bar.gt-now { background: #2e6b3a; }
  .gt-cam-bars-label { font-size: 11px; color: #8a978c; margin-top: 2px; }
  .gt-cam-err { font-size: 13px; color: #b3261e; margin-top: 4px; }
  .gt-cams-foot { font-size: 12px; color: #6b776d; margin-top: 8px; }
</style>
<script>
(function () {
  var URL_ = __RELAY_URL__;
  var TOKEN = __RELAY_TOKEN__;
  var list = document.getElementById('gt-cams-list');
  var asof = document.getElementById('gt-cams-asof');
  var DOT = ' ' + String.fromCharCode(183) + ' ';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function clock(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d)) return '';
    var h = d.getHours(), m = d.getMinutes();
    return ((h % 12) || 12) + ':' + (m < 10 ? '0' : '') + m + (h < 12 ? ' AM' : ' PM');
  }
  function ageMin(iso) {
    var d = new Date(iso);
    return isNaN(d) ? Infinity : (Date.now() - d.getTime()) / 60000;
  }
  function pitWord(k) { return k.replace(/^pit(\d+)$/i, 'Pit $1'); }

  function card(c) {
    var html = '<div class="gt-cam">';
    html += '<a class="gt-cam-name" target="_blank" rel="noopener" href="' + esc(c.url) + '">'
          + esc(c.name) + '</a>';
    html += '<span class="gt-cam-tag' + (c.open ? '' : ' gt-cam-closed') + '">'
          + (c.open ? 'receiving' : 'closed') + '</span>';
    if (c.at == null) {
      html += '<div class="gt-cam-line">nothing seen yet</div>';
      if (c.error) html += '<div class="gt-cam-err">' + esc(c.error) + '</div>';
      return html + '</div>';
    }
    var wait = c.wait_min || 0;
    var cls = wait <= 5 ? 'gt-w0' : (wait <= 20 ? 'gt-w1' : 'gt-w2');
    html += '<div class="gt-cam-wait ' + cls + '">'
          + (c.line ? 'about ' + wait + ' min wait' : 'no line') + '</div>';
    var parts = [c.line + (c.line === 1 ? ' truck' : ' trucks') + ' in line'];
    Object.keys(c.pits || {}).forEach(function (k) {
      parts.push(pitWord(k) + ' ' + (c.pits[k] ? 'busy' : 'open'));
    });
    html += '<div class="gt-cam-line">' + esc(parts.join(DOT)) + '</div>';
    html += '<div class="gt-cam-pace">' + esc(c.per_truck) + ' min per truck, from '
          + esc(c.source) + DOT + 'seen ' + clock(c.at) + '</div>';
    if (c.error) html += '<div class="gt-cam-err">last read failed: ' + esc(c.error) + '</div>';
    if (c.today && c.today.length > 1) {
      var max = 1;
      c.today.forEach(function (p) { if (p.line > max) max = p.line; });
      html += '<div class="gt-cam-bars">';
      c.today.forEach(function (p, i) {
        var h = Math.max(2, Math.round(34 * p.line / max));
        html += '<div class="gt-cam-bar' + (i === c.today.length - 1 ? ' gt-now' : '')
              + '" style="height:' + h + 'px" title="' + clock(p.t) + ': ' + p.line + ' in line"></div>';
      });
      html += '</div><div class="gt-cam-bars-label">today, ' + clock(c.today[0].t) + ' to '
            + clock(c.today[c.today.length - 1].t) + ', tallest bar = ' + max + '</div>';
    }
    return html + '</div>';
  }

  function render(data) {
    var cams = (data && data.cameras) || [];
    if (!cams.length) {
      list.innerHTML = '<div class="gt-cam">No cameras reporting yet.</div>';
      asof.textContent = '';
      return;
    }
    list.innerHTML = cams.map(card).join('');
    var age = ageMin(data.generated_at);
    var anyOpen = cams.some(function (c) { return c.open; });
    if (age > 15 && anyOpen) {
      asof.textContent = 'last update ' + Math.round(age) + ' min ago - farm PC may be off';
      asof.className = 'gt-cams-asof gt-cams-stale';
    } else {
      asof.textContent = 'updated ' + clock(data.generated_at);
      asof.className = 'gt-cams-asof';
    }
  }

  function pull() {
    if (!URL_ || !window.fetch) {
      list.innerHTML = '<div class="gt-cam">Camera relay is not configured.</div>';
      return;
    }
    fetch(URL_.replace(/\/+$/, '') + '/cameras', {
      cache: 'no-store',
      headers: { 'Authorization': 'Bearer ' + TOKEN }
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data) render(data);
        else asof.textContent = 'could not reach the relay';
      })
      .catch(function () { asof.textContent = 'could not reach the relay'; });
  }

  pull();
  setInterval(pull, 60 * 1000);
})();
</script>
"""


def build(url: str, token: str) -> str:
    return (BLOCK.replace("__RELAY_URL__", json.dumps(url))
                 .replace("__RELAY_TOKEN__", json.dumps(token)))


def main() -> None:
    cfg = read_json(RELAY, {})
    if not cfg.get("url") or not cfg.get("read_token"):
        sys.exit(f"{RELAY} needs \"url\" and \"read_token\"")
    html = build(cfg["url"], cfg["read_token"])
    if any(ord(c) > 127 for c in html):
        sys.exit("refusing to write: the result is not pure ASCII")
    OUTPUT.write_text(html, encoding="ascii", newline="\n")
    print(f"wrote {OUTPUT}  ({OUTPUT.stat().st_size / 1024:,.1f} KB)")
    print()
    print("  Paste this into a Code block on a PASSWORD-PROTECTED Squarespace page.")
    print("  It carries the relay's read token - never put it on a public page.")


if __name__ == "__main__":
    main()
