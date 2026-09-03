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
from jd_fleet import fleet_positions, now_iso, refresh_token  # noqa: E402

SECRETS = pathlib.Path.home() / ".grain-map-secrets"
RELAY = SECRETS / "relay.json"
STATE = SECRETS / "relay-state.json"

USER_AGENT = "grain-map/1.0 (farm hauling map; contact github.com/Superspyn)"


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


def main() -> None:
    cfg = load_relay()
    token = refresh_token()

    vehicles = fleet_positions(token)
    road = [v for v in vehicles
            if v["kind"] in ("semi", "pickup")
            and v["lat"] is not None and v["lon"] is not None]
    if not road:
        sys.exit("no road vehicles with a position - nothing to push")

    # The newest report in the batch stands in for the snapshot: if it has not
    # moved, Deere has not republished and there is nothing new to send.
    newest = max((v.get("at") or "") for v in road)
    if newest and newest == last_snapshot():
        print(f"unchanged since {newest} - not pushing")
        return

    payload = {"generated_at": now_iso(), "newest_report": newest, "trucks": road}
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

    STATE.write_text(json.dumps({"snapshot": newest}), encoding="utf-8")
    try:
        os.chmod(STATE, 0o600)
    except OSError:
        pass

    semis = sum(1 for v in road if v["kind"] == "semi")
    print(f"pushed {len(road)} vehicles ({semis} semis), newest report {newest}")
    print(f"  relay said: {result}")


if __name__ == "__main__":
    main()
