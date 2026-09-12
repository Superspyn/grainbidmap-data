"""Push truck positions to the private Cloudflare relay.

    python dev/jd_push.py

Reads Deere's ISO 15143-3 fleet feed and PUTs the road vehicles to the Worker,
which the password-protected map then fetches. Nothing here touches the repo.

Deere republishes that feed about every fifteen minutes - measured, not
assumed: 45 requests over 45 minutes returned four distinct snapshots at
15m32s, 15m07s and 15m06s apart. So this only pushes when snapshotTime has
actually moved, and running it every five minutes catches each new snapshot
within five minutes of publication without republishing identical data.

Configuration lives beside the Deere credentials, outside this public repo, in
    %USERPROFILE%\\.grain-map-secrets\\relay.json

    {
      "url": "https://your-worker.workers.dev",
      "push_token": "..."
    }
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import jd_idle  # noqa: E402
import jd_notify  # noqa: E402
import jd_trail  # noqa: E402
from jd_fleet import (api, connected_orgs, equipment_index,  # noqa: E402
                      fleet_positions, now_iso, refresh_token)

SECRETS = pathlib.Path.home() / ".grain-map-secrets"
RELAY = SECRETS / "relay.json"
STATE = SECRETS / "relay-state.json"

USER_AGENT = "grain-map/1.0 (farm hauling map; contact github.com/Superspyn)"

# Map pins, read once, so an alert can say "stopped at Heartland Alden"
# rather than quoting latitude and longitude at someone in a truck.
PINS = jd_notify.load_pins(
    pathlib.Path(__file__).resolve().parent.parent / "grain-trucking-map.html")


def load_relay() -> dict:
    if not RELAY.exists():
        sys.exit(
            f"No relay config found.\n"
            f"  Create {RELAY} containing:\n"
            '    {"url": "https://your-worker.workers.dev", "push_token": "..."}'
        )
    cfg = json.loads(RELAY.read_text(encoding="utf-8-sig"))
    for key in ("url", "push_token"):
        if not cfg.get(key):
            sys.exit(f"{RELAY} is missing {key!r}")
    return cfg


def last_snapshot() -> str | None:
    if not STATE.exists():
        return None
    try:
        return json.loads(STATE.read_text(encoding="utf-8")).get("snapshot")
    except (json.JSONDecodeError, OSError):
        return None


def signature(road: list[dict], newest: str) -> str:
    """What the page would draw differently. The newest report time alone is
    not enough now that each truck also carries how long it has been stopped:
    that can change - a heartbeat revealing a truck was already parked, or an
    engine starting - on a snapshot Deere has not otherwise republished."""
    return json.dumps([newest] + sorted(
        f"{t.get('vin') or t.get('name')}|{t.get('since')}|"
        f"{int(bool(t.get('moving')))}{int(bool(t.get('engine_on')))}"
        f"{int(bool(t.get('since_min')))}|{len(t.get('trail') or [])}"
        for t in road))


def main() -> None:
    cfg = load_relay()
    token = refresh_token()

    vehicles = fleet_positions(token)
    road = [v for v in vehicles
            if v["kind"] in ("semi", "pickup")
            and v["lat"] is not None and v["lon"] is not None]
    if not road:
        sys.exit("no road vehicles with a position - nothing to push")

    # Always track, even when nothing gets pushed. A truck seen to move
    # between two readings gets an exact arrival time for free, and this is
    # the only thing reading the feed often enough to catch that.
    jd_idle.track(road)

    # Then pin down the ones the feed cannot date, from position history.
    # A few per run: the work shrinks to nothing as trucks are either
    # refined or observed moving.
    refined = crumbed = 0
    stops: list[dict] = []
    try:
        index = equipment_index(token, [str(o["id"]) for o in connected_orgs(token)])
        refined = jd_idle.refine(api, token, road, index)
        if refined:
            jd_idle.track(road)      # re-annotate from what history settled
        # Breadcrumbs: the path driven, and any long stop along it.
        crumbed, stops = jd_trail.update(api, token, road, index)
        if stops:
            # Only the stops found on THIS run. jd_trail dedupes, so one
            # already reported never comes round again.
            texted = jd_notify.notify_stops(stops, PINS)
            if texted:
                print(f"  texted {texted} stop(s)")
    except Exception as exc:  # noqa: BLE001
        # History and trails are improvements, not dependencies. Losing them
        # must not stop positions reaching the map.
        print(f"  (history/trail step skipped: {type(exc).__name__}: {exc})")

    newest = max((v.get("at") or "") for v in road)
    sig = signature(road, newest)
    if sig == last_snapshot():
        print(f"unchanged since {newest} - not pushing")
        return

    payload = {"generated_at": now_iso(), "newest_report": newest, "trucks": road,
               "stops": jd_trail.recent_events(24),
               "stop_minutes": jd_trail.IDLE_MIN}
    request = urllib.request.Request(
        cfg["url"], data=json.dumps(payload).encode(), method="PUT")
    request.add_header("Authorization", "Bearer " + cfg["push_token"])
    request.add_header("Content-Type", "application/json")
    # Cloudflare rejects urllib's default agent with its own 1010 bot-signature
    # error before the Worker ever runs, which reads as the token being wrong.
    # Any honest agent is accepted; this one says who we are.
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = response.read().decode()
    except urllib.error.HTTPError as exc:
        sys.exit(f"relay refused the push: HTTP {exc.code} "
                 f"{exc.read().decode()[:120]}")

    STATE.write_text(json.dumps({"snapshot": sig}), encoding="utf-8")
    try:
        os.chmod(STATE, 0o600)
    except OSError:
        pass

    semis = sum(1 for v in road if v["kind"] == "semi")
    moving = sum(1 for v in road if v.get("moving"))
    idling = sum(1 for v in road if v.get("engine_on") and not v.get("moving"))
    exact = sum(1 for v in road if not v.get("since_min"))
    print(f"pushed {len(road)} vehicles ({semis} semis), newest report {newest}")
    print(f"  {moving} moving, {idling} idling with the engine on, "
          f"{len(road) - moving} stopped")
    print(f"  {exact}/{len(road)} have an exact stopped-since time"
          f"{f', {refined} refined from history this run' if refined else ''}")
    print(f"  breadcrumbs read for {crumbed} vehicles"
          f"{f', {len(stops)} new stop(s) over {jd_trail.IDLE_MIN:.0f} min' if stops else ''}"
          f"; {len(payload['stops'])} stop(s) in the last 24 h")
    print(f"  relay said: {result}")


if __name__ == "__main__":
    main()
