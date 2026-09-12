"""Text a phone when a truck has stood still for too long.

Called by dev/jd_push.py whenever jd_trail finds a new long stop. Two ways
to send, both arriving as an ordinary text with no app to install.

**email** - free, and the one to start with. Every US carrier accepts mail
at a gateway address and turns it into a text, so this costs nothing beyond
a Gmail app password. At roughly one alert a day that is the whole bill.
Carriers have been quietly retiring these gateways, so if texts stop
arriving, that is the first thing to suspect - switch to twilio.

    {
      "provider": "email",
      "username": "you@gmail.com",
      "app_password": "abcd efgh ijkl mnop",
      "to": [{"phone": "5155559876", "carrier": "verizon"}],
      "quiet_hours": [21, 6],
      "max_per_hour": 6
    }

**twilio** - paid, and worth it if the gateway proves flaky. The trial
credit covers years at this volume; the standing cost is the phone number.

    {
      "provider": "twilio",
      "account_sid": "AC...",
      "auth_token": "...",
      "from": "+15155550123",
      "to": ["+15155559876"]
    }

Either way the file lives OUTSIDE this public repo, at
%USERPROFILE%\\.grain-map-secrets\\sms.json, and is written by hand.

`quiet_hours` is [start, end] in local time and may be omitted; a stop found
inside it is recorded on the map but not texted, because a truck parked at
the yard overnight is not worth waking anyone for. `max_per_hour` is a
backstop: if something goes wrong upstream and a hundred stops appear at
once, this stops a hundred texts going out. Both have sane defaults.

Nothing is ever sent unless that file exists. There is a --dry-run so the
wiring can be tested without a message leaving the machine.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

SECRETS = pathlib.Path.home() / ".grain-map-secrets"
CONFIG = SECRETS / "sms.json"
STATE = SECRETS / "sms-state.json"

TWILIO = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

DEFAULT_MAX_PER_HOUR = 6

# Pins from the public map, for saying where a truck stopped rather than
# quoting coordinates at someone. Same regex the scraper's matcher uses.
_PIN_RE = re.compile(
    r"\{\s*name:\s*'((?:[^'\\]|\\.)*)'\s*,\s*type:\s*'([^']*)'\s*,"
    r"(?:\s*company:\s*'((?:[^'\\]|\\.)*)'\s*,)?"
    r"\s*lat:\s*(-?[\d.]+)\s*,\s*lng:\s*(-?[\d.]+)")

# How close a stop has to be to a pin or field to be named after it.
NEAR_PIN_M = 600.0
NEAR_FIELD_M = 400.0


# Carrier mail-to-SMS gateways, US. Mail to <number>@<domain> arrives as an
# ordinary text. Free, and the reason the email provider exists.
CARRIERS = {
    "verizon": "vtext.com",
    "att": "txt.att.net",
    "at&t": "txt.att.net",
    "tmobile": "tmomail.net",
    "t-mobile": "tmomail.net",
    "sprint": "messaging.sprintpcs.com",
    "uscellular": "email.uscc.net",
    "us cellular": "email.uscc.net",
    "cricket": "sms.cricketwireless.net",
    "boost": "sms.myboostmobile.com",
    "googlefi": "msg.fi.google.com",
    "google fi": "msg.fi.google.com",
    "mint": "mailmymobile.net",
    "consumer cellular": "mailmymobile.net",
    "straight talk": "vtext.com",
    "tracfone": "mmst5.tracfone.com",
}

REQUIRED = {
    "twilio": ("account_sid", "auth_token", "from", "to"),
    "email": ("username", "app_password", "to"),
}


def sms_address(entry) -> str | None:
    """A gateway address from either a plain string or {phone, carrier}."""
    if isinstance(entry, str):
        return entry.strip() or None
    if not isinstance(entry, dict):
        return None
    digits = re.sub(r"\D", "", str(entry.get("phone") or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    domain = CARRIERS.get(str(entry.get("carrier") or "").strip().lower())
    if len(digits) != 10 or not domain:
        return None
    return f"{digits}@{domain}"


def load_config() -> dict | None:
    if not CONFIG.exists():
        return None
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  (sms.json unreadable: {exc})")
        return None
    provider = str(cfg.get("provider") or "twilio").strip().lower()
    if provider not in REQUIRED:
        print(f"  (sms.json has an unknown provider {provider!r} - not texting)")
        return None
    cfg["provider"] = provider
    for key in REQUIRED[provider]:
        if not cfg.get(key):
            print(f"  (sms.json is missing {key!r} for provider "
                  f"{provider} - not texting)")
            return None
    if isinstance(cfg["to"], (str, dict)):
        cfg["to"] = [cfg["to"]]
    if provider == "email":
        resolved = [sms_address(entry) for entry in cfg["to"]]
        bad = [e for e, r in zip(cfg["to"], resolved) if not r]
        if bad:
            print(f"  (cannot work out a text address for {bad} - "
                  f"known carriers: {', '.join(sorted(set(CARRIERS)))})")
            return None
        cfg["to"] = resolved
    return cfg


def _metres(lat1, lon1, lat2, lon2) -> float:
    import math
    dlat = (lat2 - lat1) * 111_320.0
    dlon = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


def place_name(lat: float, lon: float, fields: list[dict],
               pins: list[dict]) -> str | None:
    """The nearest elevator or field, so a text can say where.

    Checked against the map's own pins and the farm's own field centroids -
    no geocoding service, no request, no cost.
    """
    best = None
    for pin in pins:
        d = _metres(lat, lon, pin["lat"], pin["lng"])
        if d <= NEAR_PIN_M and (best is None or d < best[0]):
            best = (d, pin["name"])
    for field in fields:
        if field.get("lat") is None:
            continue
        d = _metres(lat, lon, field["lat"], field["lon"])
        if d <= NEAR_FIELD_M and (best is None or d < best[0]):
            best = (d, field.get("name") or "a field")
    return best[1] if best else None


def load_pins(html: pathlib.Path) -> list[dict]:
    if not html.exists():
        return []
    text = html.read_text(encoding="utf-8", errors="replace")
    return [{"name": m.group(1).replace("\\'", "'").strip(),
             "lat": float(m.group(4)), "lng": float(m.group(5))}
            for m in _PIN_RE.finditer(text)]


def _state() -> dict:
    if not STATE.exists():
        return {"sent": []}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"sent": []}


def _save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state), encoding="utf-8")
    try:
        os.chmod(STATE, 0o600)
    except OSError:
        pass


def in_quiet_hours(cfg: dict, when: _dt.datetime) -> bool:
    window = cfg.get("quiet_hours")
    if not window or len(window) != 2:
        return False
    start, end = int(window[0]), int(window[1])
    hour = when.astimezone().hour
    # A window like [21, 6] wraps around midnight.
    return start <= hour or hour < end if start > end else start <= hour < end


def compose(stop: dict, where: str | None) -> str:
    minutes = int(stop.get("minutes") or 0)
    length = (f"{minutes} min" if minutes < 60
              else f"{minutes // 60} h {minutes % 60} min")
    when = stop.get("start") or ""
    try:
        local = (_dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
                 .astimezone().strftime("%-I:%M %p"))
    except (ValueError, TypeError):
        try:
            local = (_dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
                     .astimezone().strftime("%I:%M %p").lstrip("0"))
        except (ValueError, TypeError):
            local = "?"
    return (f"{stop.get('name')} stopped {length} from {local}"
            + (f" at {where}" if where else "")
            + f" ({stop.get('y'):.4f},{stop.get('x'):.4f})")


def send(cfg: dict, body: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"  [dry run] would text via {cfg.get('provider')}: {body}")
        return True
    return (send_email(cfg, body) if cfg.get("provider") == "email"
            else send_twilio(cfg, body))


def send_email(cfg: dict, body: str) -> bool:
    """Through a carrier's mail-to-SMS gateway.

    No subject line: gateways prepend it to the message body, so a subject
    just eats into the 160 characters that actually get delivered.
    """
    import smtplib
    from email.message import EmailMessage

    host = cfg.get("smtp_host") or "smtp.gmail.com"
    port = int(cfg.get("smtp_port") or 587)
    ok = True
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(cfg["username"], cfg["app_password"])
            for address in cfg["to"]:
                message = EmailMessage()
                message["From"] = cfg["username"]
                message["To"] = address
                message.set_content(body)
                try:
                    server.send_message(message)
                except Exception as exc:  # noqa: BLE001
                    print(f"  (gateway refused {address}: {type(exc).__name__})")
                    ok = False
    except smtplib.SMTPAuthenticationError:
        print("  (Gmail rejected the app password - it must be an App Password, "
              "not the account password, and 2-step verification must be on)")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not reach {host}: {type(exc).__name__}: {exc})")
        return False
    return ok


def send_twilio(cfg: dict, body: str) -> bool:
    url = TWILIO.format(sid=urllib.parse.quote(cfg["account_sid"]))
    ok = True
    for number in cfg["to"]:
        data = urllib.parse.urlencode(
            {"From": cfg["from"], "To": number, "Body": body}).encode()
        request = urllib.request.Request(url, data=data, method="POST")
        raw = f'{cfg["account_sid"]}:{cfg["auth_token"]}'.encode()
        request.add_header("Authorization",
                           "Basic " + base64.b64encode(raw).decode())
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:160]
            print(f"  (text to {number[-4:]} refused: HTTP {exc.code} {detail})")
            ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"  (text to {number[-4:]} failed: {type(exc).__name__})")
            ok = False
    return ok


def load_fields() -> list[dict]:
    fleet = SECRETS / "fleet.json"
    if not fleet.exists():
        return []
    try:
        return json.loads(fleet.read_text(encoding="utf-8")).get("fields", [])
    except (json.JSONDecodeError, OSError):
        return []


def notify_stops(stops: list[dict], pins: list[dict],
                 fields: list[dict] | None = None,
                 dry_run: bool = False) -> int:
    """Text each new stop. Returns how many messages went out."""
    cfg = load_config()
    if not cfg or not stops:
        return 0
    if fields is None:
        fields = load_fields()
    now = _dt.datetime.now(_dt.timezone.utc)
    state = _state()
    hour_ago = now - _dt.timedelta(hours=1)
    recent = [s for s in state["sent"]
              if _dt.datetime.fromisoformat(s.replace("Z", "+00:00")) >= hour_ago]
    cap = int(cfg.get("max_per_hour") or DEFAULT_MAX_PER_HOUR)

    if in_quiet_hours(cfg, now):
        print(f"  ({len(stops)} stop(s) inside quiet hours - not texting)")
        state["sent"] = recent
        _save_state(state)
        return 0

    # One counter, not two: `recent` already holds this run's sends once they
    # happen, so adding `sent` to it as well spent the budget twice as fast.
    used = len(recent)
    sent = 0
    for stop in stops:
        if used >= cap:
            print(f"  (hit the {cap}/hour text cap - the rest are on the map only)")
            break
        where = place_name(stop.get("y"), stop.get("x"), fields, pins)
        if send(cfg, compose(stop, where), dry_run):
            sent += 1
            used += 1
            if not dry_run:
                recent.append(now.isoformat(timespec="seconds").replace("+00:00", "Z"))
    state["sent"] = recent
    _save_state(state)
    return sent


def main() -> None:
    """Send a test message, so the wiring can be proved before relying on it.

        python dev/jd_notify.py --test          really sends one
        python dev/jd_notify.py --test --dry-run  prints what it would send
    """
    dry = "--dry-run" in sys.argv
    cfg = load_config()
    if not cfg:
        sys.exit(f"No {CONFIG} - nothing to test. See the docstring for the shape.")
    fleet = SECRETS / "fleet.json"
    fields = (json.loads(fleet.read_text(encoding="utf-8")).get("fields", [])
              if fleet.exists() else [])
    pins = load_pins(pathlib.Path(__file__).resolve().parent.parent
                     / "grain-trucking-map.html")
    print(f"config ok: provider {cfg['provider']}, {len(cfg['to'])} recipient(s) "
          f"({', '.join(str(t) for t in cfg['to'])}), "
          f"{len(pins)} map pins and {len(fields)} fields for naming places")
    example = {"name": "Test Truck", "minutes": 17, "y": 43.0821, "x": -93.8239,
               "start": _dt.datetime.now(_dt.timezone.utc).isoformat()}
    where = place_name(example["y"], example["x"], fields, pins)
    print(f"nearest named place to the example: {where or '(nothing within range)'}")
    if "--test" in sys.argv:
        send(cfg, "Grain map test: " + compose(example, where), dry)
        print("sent" if not dry else "dry run only - nothing left this machine")
    else:
        print("add --test to send one, --dry-run to only print it")


if __name__ == "__main__":
    main()
