"""Watch elevator line cameras and say how long the wait is.

    python dev/cam_watch.py              # one pass over every camera (the scheduled task)
    python dev/cam_watch.py --report     # what each camera has seen today
    python dev/cam_watch.py --frame path.jpg --camera jewell   # detect on a saved frame

Built like the bid scraper and kept apart from the map on purpose: the farm
PC runs it, the results land in a log on the PC, and nothing reaches the
map until that is decided separately. What does leave the PC is a small
summary - counts, wait, which pace was used - pushed to the private relay's
/cameras key, where the paste-in block from dev/cam_build_block.py reads it.

What one pass does, per camera:

  1. Fetch the camera page. POET's truckline pages carry the current frame
     inline as a base64 JPEG, refreshed every ten seconds server-side, so a
     plain GET is a fresh still - no stream to decode, no login, and their
     robots.txt permits it.
  2. Find the trucks in it with a local detector (see `detect`). The frame
     never leaves the machine.
  3. Count how many stand in the LINE region and whether each PIT region is
     occupied. Regions are polygons in cameras.json, drawn once per camera
     from a real frame. A detection counts where its wheels are (bottom
     centre of the box), which is what puts a truck on the apron rather
     than the building behind it.
  4. Append the counts to the log, keep the frame and an annotated copy so
     the count can be eyeballed, and work out the wait.

The wait is trucks in line times minutes per truck. The minutes per truck
are MEASURED, not guessed, two ways: from the camera, by how often a pit
empties and refills (each cycle is one truck served); and from the farm's
own trucks, whose breadcrumbs say to the second how long each visit to
that elevator took. Before either has data there is a default, and the
report says which of the three it used.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import html
import json
import pathlib
import re
import ssl
import statistics
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, iso, metres, now_iso, parse_iso, read_json, write_private  # noqa: E402

CONFIG = pathlib.Path(__file__).resolve().parent / "config" / "cameras.json"
STATE = SECRETS / "cam-state.json"
FRAMES = SECRETS / "cam-frames"

USER_AGENT = "grain-map/1.0 (farm hauling map; contact github.com/Superspyn)"

# Detections the counter believes. COCO's classes for road vehicles; a grain
# truck reads as "truck" almost always, and occasionally "bus" when only the
# box is visible. Cars are pickups and the odd employee vehicle, which the
# size floor removes rather than the class.
VEHICLE_CLASSES = {"truck", "bus", "car"}
MIN_CONFIDENCE = 0.30

# The camera is 704x576 and a truck under a pit is 50-odd pixels tall. At
# the detector's default 640 it found nothing at all in a frame with a
# truck plainly at the pit; upsampled to 1280 it finds it at 0.41. Trucks
# queued on the apron are nearer and larger, so this is the hard case.
DETECT_SIZE = 1280

# Which YOLO. "n" (6 MB) caught trucks near the camera and missed the far
# ones: 2 of 5 staged at CHS Fairmont, none of the queue at the far end of
# Jewell's probe lane. "s" (22 MB) found all five and the queue, in under
# half a second a frame on this PC. Fetched on first use if missing.
WEIGHTS = "yolov8s.pt"

# How much history to keep, and how far back "recent" reaches when working
# out the pace of the line.
KEEP_DAYS = 14
PACE_WINDOW_MIN = 90.0

# A visit by one of the farm's own trucks: breadcrumbs within this distance
# of the pin, split into separate visits by a gap longer than this.
VISIT_RADIUS_M = 400.0
VISIT_GAP_MIN = 20.0


# ---------------------------------------------------------------------------
# geometry

def point_in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    """Ray casting. Polygon as [[x, y], ...] in image pixels."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xt = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xt:
                inside = not inside
    return inside


def foot(box: dict) -> tuple[float, float]:
    """Where the wheels are: bottom centre of a box {x1, y1, x2, y2}."""
    return (box["x1"] + box["x2"]) / 2, box["y2"]


def count_regions(boxes: list[dict], camera: dict) -> dict:
    """Trucks in the line and at each pit, from detections and the camera's
    regions. A box has to be tall enough to be a truck at that distance -
    the floor is per camera, set from a real frame - and it is placed by
    its feet. A pit region wins over the line region, so a truck under the
    building is "at the pit", not "in line"."""
    floor = float(camera.get("min_box_height", 0))
    # Width too, for cameras far from the road: at Shell Rock a semi and a
    # pickup are both ~30 px tall, but the semi is 46 px long and the
    # pickup 19. Optional - a camera without it filters on height alone.
    min_w = float(camera.get("min_box_width", 0))
    pits = camera.get("pits") or {}
    line_poly = camera.get("line") or []
    # Places inside the line where a vehicle is never a truck in line: the
    # employee parking on the median at Gowrie counted two cars as trucks
    # waiting, because at 3840 px wide an SUV clears any sensible size floor.
    exclude = camera.get("exclude") or []
    at_pit = {name: False for name in pits}
    in_line = 0
    kept = []
    for b in boxes:
        if b.get("cls") not in VEHICLE_CLASSES or b.get("conf", 0) < MIN_CONFIDENCE:
            continue
        if (b["y2"] - b["y1"]) < floor or (b["x2"] - b["x1"]) < min_w:
            continue
        fx, fy = foot(b)
        if any(point_in_polygon(fx, fy, poly) for poly in exclude):
            continue
        where = None
        for name, poly in pits.items():
            if point_in_polygon(fx, fy, poly):
                at_pit[name] = True
                where = "pit:" + name
                break
        if where is None and line_poly and point_in_polygon(fx, fy, line_poly):
            in_line += 1
            where = "line"
        kept.append(dict(b, where=where))
    return {"line": in_line, "pits": at_pit, "boxes": kept}


# ---------------------------------------------------------------------------
# the camera

EXTRA_CA = pathlib.Path(__file__).resolve().parent / "config" / "extra-ca.pem"
_tls = None


def tls_context() -> ssl.SSLContext:
    """The default verifying context, plus any public intermediate certs in
    config/extra-ca.pem. CHS's camera host sends its leaf without the
    Entrust intermediate that signed it; a browser fetches the missing link
    itself, Python's OpenSSL does not, and the fetch failed with "unable to
    get local issuer certificate" until the intermediate was on hand.
    Verification stays on - this only supplies what the server forgot."""
    global _tls
    if _tls is None:
        _tls = ssl.create_default_context()
        if EXTRA_CA.exists():
            _tls.load_verify_locations(cafile=str(EXTRA_CA))
    return _tls


def parse_frame(page: str) -> dict:
    """The inline frame out of a camera page. POET declares it image/jpeg;
    CHS declares image/png and sends JPEG bytes anyway, so the declared
    type is ignored and the bytes are what they are."""
    m = re.search(r'src="data:image/(?:jpeg|jpg|png);base64,([^"]+)"', page)
    if not m:
        raise RuntimeError("no inline JPEG on the camera page")
    jpeg = base64.b64decode(html.unescape(m.group(1)))
    exif = re.search(rb"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", jpeg[:4096])
    cap = re.search(r"(\d{1,2}:\d{2} [AP]M [A-Z]{3,4})", page)
    return {"jpeg": jpeg,
            "camera_time": ("%s-%s-%sT%s:%s:%s" % tuple(g.decode() for g in exif.groups())) if exif else None,
            "page_time": cap.group(1) if cap else None}


def fetch_frame(url: str, timeout: float = 20.0) -> dict:
    """The current frame and its timestamps. Returns {jpeg, page_time,
    camera_time}; camera_time comes from the JPEG's own EXIF, page_time
    from the page's caption, either may be None."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=tls_context()) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    if ctype.startswith("image/") or raw[:3] == b"\xff\xd8\xff":
        # New Vision's ipcamlive snapshots are the JPEG itself, no page.
        exif = re.search(rb"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", raw[:4096])
        return {"jpeg": raw, "page_time": None,
                "camera_time": ("%s-%s-%sT%s:%s:%s" % tuple(g.decode() for g in exif.groups())) if exif else None}
    return parse_frame(raw.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# the detector

_model = None


def detect(jpeg: bytes) -> list[dict]:
    """Vehicles in a frame as [{cls, conf, x1, y1, x2, y2}], from a detector
    that runs on this machine. Ultralytics' YOLO (COCO classes) - a one-time
    install; nothing about the frame goes anywhere. A missing detector is a
    clear error, not a silent zero."""
    global _model
    try:
        import numpy as np  # noqa: F401
        from ultralytics import YOLO
    except ImportError as exc:
        raise RuntimeError(
            "the truck detector is not installed on this machine: "
            "pip install ultralytics  (downloads torch; the yolov8n weights "
            "fetch on first run)") from exc
    if _model is None:
        weights = pathlib.Path(__file__).resolve().parent / "config" / WEIGHTS
        if not weights.exists():
            # From Ultralytics' own GitHub release, into this folder.
            # Kept out of the repo (it is public and this is a binary).
            from ultralytics.utils.downloads import attempt_download_asset
            attempt_download_asset(str(weights))
        _model = YOLO(str(weights))
    import io
    from PIL import Image
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    results = _model.predict(image, verbose=False, conf=MIN_CONFIDENCE, imgsz=DETECT_SIZE)
    boxes = []
    for r in results:
        names = r.names
        for b in r.boxes:
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0].tolist()]
            boxes.append({"cls": names[int(b.cls[0])], "conf": round(float(b.conf[0]), 3),
                          "x1": round(x1, 1), "y1": round(y1, 1), "x2": round(x2, 1), "y2": round(y2, 1)})
    return boxes


def annotate(jpeg: bytes, counted: dict, camera: dict, out: pathlib.Path) -> None:
    """The frame with the regions and the counted boxes drawn on, for a
    person to check the count against. Best effort: no PIL, no picture."""
    try:
        import io
        from PIL import Image, ImageDraw
    except ImportError:
        return
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    draw = ImageDraw.Draw(image)
    if camera.get("line"):
        draw.polygon([tuple(p) for p in camera["line"]], outline=(60, 160, 60))
    for poly in camera.get("exclude") or []:
        draw.polygon([tuple(p) for p in poly], outline=(200, 60, 60))
    for name, poly in (camera.get("pits") or {}).items():
        draw.polygon([tuple(p) for p in poly], outline=(200, 140, 30))
    for b in counted["boxes"]:
        colour = {"line": (60, 160, 60), None: (150, 150, 150)}.get(
            b["where"], (200, 140, 30))
        draw.rectangle([b["x1"], b["y1"], b["x2"], b["y2"]], outline=colour, width=2)
        draw.text((b["x1"] + 2, b["y1"] + 2), f"{b['cls']} {b['conf']:.2f}", fill=colour)
    draw.text((6, 6), f"line {counted['line']}  " +
              "  ".join(f"{k} {'busy' if v else 'open'}" for k, v in counted["pits"].items()),
              fill=(255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, quality=85)


# ---------------------------------------------------------------------------
# pace: minutes per truck

def pit_cycles(history: list[dict], since: _dt.datetime) -> tuple[int, float]:
    """(trucks served, minutes observed) from pit occupancy flipping
    busy -> open -> busy. Each refill is one truck through. Counted per pit
    and summed, over readings newer than `since`."""
    rows = [h for h in history if (parse_iso(h["t"]) or since) >= since]
    if len(rows) < 2:
        return 0, 0.0
    served = 0
    pits = set()
    for h in rows:
        pits.update((h.get("pits") or {}).keys())
    for pit in pits:
        prev = None
        for h in rows:
            busy = bool((h.get("pits") or {}).get(pit))
            if prev is False and busy:
                served += 1
            prev = busy
    span = (parse_iso(rows[-1]["t"]) - parse_iso(rows[0]["t"])).total_seconds() / 60
    return served, span


def own_visits(pin_lat: float, pin_lon: float, trails: dict) -> list[dict]:
    """Every visit one of the farm's trucks made to this elevator, from
    breadcrumbs within VISIT_RADIUS_M of the pin: first crumb inside to last
    crumb inside, a gap over VISIT_GAP_MIN starting a new visit. The length
    of a visit is the truck's whole time on the place - waiting, dumping,
    leaving - which is what a driver means by "how long did it take"."""
    visits = []
    for vin, rec in (trails.get("vehicles") or {}).items():
        run: list[dict] = []
        for p in rec.get("points") or []:
            inside = metres(p["y"], p["x"], pin_lat, pin_lon) <= VISIT_RADIUS_M
            if inside:
                if run:
                    gap = (parse_iso(p["t"]) - parse_iso(run[-1]["t"])).total_seconds() / 60
                    if gap > VISIT_GAP_MIN:
                        visits.append(_visit(vin, run))
                        run = []
                run.append(p)
        if run:
            visits.append(_visit(vin, run))
    return [v for v in visits if v["minutes"] >= 2]


def _visit(vin: str, run: list[dict]) -> dict:
    a, b = parse_iso(run[0]["t"]), parse_iso(run[-1]["t"])
    return {"vin": vin, "start": run[0]["t"], "end": run[-1]["t"],
            "minutes": round((b - a).total_seconds() / 60, 1)}


def pace(camera: dict, history: list[dict], visits: list[dict],
         now: _dt.datetime) -> tuple[float, str]:
    """Minutes per truck and where the figure came from."""
    served, span = pit_cycles(history, now - _dt.timedelta(minutes=PACE_WINDOW_MIN))
    if served >= 2 and span > 0:
        return span / served, f"the pit turned over {served} trucks in {span:.0f} min"
    recent = [v["minutes"] for v in visits
              if (now - (parse_iso(v["end"]) or now)).days < 14]
    if recent:
        return statistics.median(recent), f"your own trucks' last {len(recent)} visit(s)"
    return float(camera.get("default_minutes_per_truck", 5)), "the default - nothing measured yet"


# ---------------------------------------------------------------------------
# the run

def load_cameras() -> list[dict]:
    cams = json.loads(CONFIG.read_text(encoding="utf-8"))
    return [c for c in cams if c.get("url")]


def views_of(camera: dict) -> list[dict]:
    """The pictures a camera entry is read from. Usually one - the entry
    itself, with its url and regions. New Vision points two cameras at
    each elevator, an inbound lane and the dump pit, so an entry may carry
    "views" instead: each with its own url and regions, counted separately
    and merged into one reading (line counts add, pit states union)."""
    views = camera.get("views")
    if not views:
        return [dict(camera, view="")]
    return [dict(v, view=v.get("id") or str(i)) for i, v in enumerate(views)]


def read_views(camera: dict, fetch=None, detector=None) -> tuple[dict, list[dict], list[tuple[str, bytes]]]:
    """Fetch and count every view of a camera. Returns the merged counts,
    the per-view counts, and the frames for annotation."""
    fetch = fetch or fetch_frame
    detector = detector or detect
    merged = {"line": 0, "pits": {}, "vehicles": 0, "camera_time": None}
    per_view, frames = [], []
    for view in views_of(camera):
        frame = fetch(view["url"])
        counted = count_regions(detector(frame["jpeg"]), view)
        merged["line"] += counted["line"]
        for name, busy in counted["pits"].items():
            merged["pits"][name] = merged["pits"].get(name, False) or busy
        merged["vehicles"] += len(counted["boxes"])
        merged["camera_time"] = merged["camera_time"] or frame.get("camera_time")
        per_view.append(dict(counted, view=view["view"]))
        frames.append((view["view"], frame["jpeg"]))
    return merged, per_view, frames


def in_hours(camera: dict, now_local: _dt.datetime) -> bool:
    hours = camera.get("hours")
    if not hours:
        return True
    start, end = int(hours[0]), int(hours[1])
    return start <= now_local.hour < end


def watch_once(cameras: list[dict], state: dict, force: bool = False) -> None:
    now = _dt.datetime.now(_dt.timezone.utc)
    for cam in cameras:
        cid = cam["id"]
        if not force and not in_hours(cam, _dt.datetime.now()):
            print(f"  {cid}: outside receiving hours")
            continue
        try:
            counted, per_view, frames = read_views(cam)
        except Exception as exc:  # noqa: BLE001
            print(f"  {cid}: {type(exc).__name__}: {exc}")
            state.setdefault("errors", []).append({"t": now_iso(), "camera": cid, "error": str(exc)[:200]})
            continue
        reading = {"t": now_iso(), "camera_time": counted["camera_time"],
                   "line": counted["line"], "pits": counted["pits"],
                   "vehicles": counted["vehicles"]}
        hist = state.setdefault("cameras", {}).setdefault(cid, [])
        hist.append(reading)
        FRAMES.mkdir(parents=True, exist_ok=True)
        for view, (vid, jpeg), one in zip(views_of(cam), frames, per_view):
            tag = f"{cid}-{vid}" if vid else cid
            (FRAMES / f"{tag}.jpg").write_bytes(jpeg)
            annotate(jpeg, one, view, FRAMES / f"{tag}-counted.jpg")
        print(f"  {cid}: {counted['line']} in line, "
              + ", ".join(f"{k} {'busy' if v else 'open'}" for k, v in counted["pits"].items())
              + f"  (frame {counted['camera_time'] or '?'})")

    horizon = now - _dt.timedelta(days=KEEP_DAYS)
    for cid, hist in (state.get("cameras") or {}).items():
        state["cameras"][cid] = [h for h in hist if (parse_iso(h["t"]) or now) >= horizon]
    state["errors"] = [e for e in state.get("errors", []) if (parse_iso(e["t"]) or now) >= horizon][-200:]
    state["updated"] = now_iso()


def summary(cameras: list[dict], state: dict, now: _dt.datetime | None = None,
            trails: dict | None = None) -> dict:
    """Everything a page needs to show the wait, and nothing it does not:
    no frames, no detection boxes, no region polygons. One entry per camera
    whether or not it has been read yet, so the page can list a camera as
    "nothing seen yet" rather than silently leaving it out."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if trails is None:
        trails = read_json(SECRETS / "trails.json", {})
    today = now.astimezone().date()
    out = []
    for cam in cameras:
        cid = cam["id"]
        hist = (state.get("cameras") or {}).get(cid) or []
        entry = {"id": cid, "name": cam.get("name", cid), "url": cam.get("url"),
                 "pin": cam.get("pin"), "hours": cam.get("hours"),
                 "open": in_hours(cam, now.astimezone()),
                 "at": None, "camera_time": None, "line": None, "pits": {},
                 "per_truck": None, "source": None, "wait_min": None,
                 "today": [], "visits": []}
        errors = [e for e in state.get("errors", []) if e.get("camera") == cid]
        if errors and (not hist or errors[-1]["t"] > hist[-1]["t"]):
            entry["error"] = errors[-1]["error"]
        if hist:
            last = hist[-1]
            visits = own_visits(cam["lat"], cam["lon"], trails) if cam.get("lat") else []
            per_truck, source = pace(cam, hist, visits, now)
            entry.update({
                "at": last["t"], "camera_time": last.get("camera_time"),
                "line": last["line"], "pits": last.get("pits") or {},
                "per_truck": round(per_truck, 1), "source": source,
                "wait_min": round(last["line"] * per_truck),
                # Today's line counts, thinned to one reading per ten minutes
                # at most so a fourteen-hour day is under a hundred points.
                "today": _thin([{"t": h["t"], "line": h["line"]} for h in hist
                                if (parse_iso(h["t"]) or now).astimezone().date() == today]),
                "visits": [{"vin": v["vin"][-6:], "start": v["start"], "minutes": v["minutes"]}
                           for v in sorted(visits, key=lambda v: v["end"])[-3:]],
            })
        out.append(entry)
    return {"generated_at": iso(now), "cameras": out}


def _thin(points: list[dict], every_min: float = 10.0) -> list[dict]:
    kept: list[dict] = []
    for p in points:
        if kept and (parse_iso(p["t"]) - parse_iso(kept[-1]["t"])).total_seconds() < every_min * 60:
            kept[-1] = p if p["line"] > kept[-1]["line"] else kept[-1]   # keep the busier reading
            continue
        kept.append(p)
    return kept


def report(cameras: list[dict], state: dict) -> None:
    for entry in summary(cameras, state)["cameras"]:
        print(f"\n{entry['name']}")
        if entry.get("error"):
            print(f"  last try failed: {entry['error']}")
        if entry["at"] is None:
            print("  nothing seen yet")
            continue
        when = parse_iso(entry["at"]).astimezone().strftime("%I:%M %p").lstrip("0")
        pits = ", ".join(f"{k} {'busy' if v else 'open'}" for k, v in entry["pits"].items())
        print(f"  {when}: {entry['line']} in line, {pits}")
        print(f"  about {entry['wait_min']} min wait at {entry['per_truck']:.1f} min/truck"
              f" - from {entry['source']}")
        if len(entry["today"]) > 1:
            line = " ".join(f"{parse_iso(h['t']).astimezone().strftime('%H:%M')}={h['line']}"
                            for h in entry["today"][-24:])
            print(f"  today: {line}")
        if entry["visits"]:
            print("  your trucks there: " + "; ".join(
                f"{v['vin']} {v['minutes']:.0f} min on {v['start'][5:10]}" for v in entry["visits"]))


# ---------------------------------------------------------------------------
# the relay

RELAY = SECRETS / "relay.json"
PUSH_EVERY_MIN = 10.0


def push(cameras: list[dict], state: dict) -> str | None:
    """PUT the summary to the private relay's /cameras key, if a relay is
    configured. Only when something the page shows has changed, or every
    ten minutes regardless so its "as of" keeps moving: a quiet afternoon
    of "0 in line" every two minutes is 400 identical writes a day, and
    the relay's free tier allows a thousand across trucks and cameras."""
    cfg = read_json(RELAY, {})
    if not cfg.get("url") or not cfg.get("push_token"):
        return None
    now = _dt.datetime.now(_dt.timezone.utc)
    body = summary(cameras, state, now)
    sig = json.dumps([[c["id"], c["line"], c["pits"], c["wait_min"], c["source"], c.get("error")]
                      for c in body["cameras"]], sort_keys=True)
    last = state.get("relay") or {}
    last_at = parse_iso(last.get("at"))
    if sig == last.get("sig") and last_at and (now - last_at).total_seconds() < PUSH_EVERY_MIN * 60:
        return "unchanged"
    request = urllib.request.Request(
        cfg["url"].rstrip("/") + "/cameras", data=json.dumps(body).encode(), method="PUT")
    request.add_header("Authorization", "Bearer " + cfg["push_token"])
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = response.read().decode()[:120]
    except (urllib.error.URLError, OSError) as exc:
        return f"relay refused: {exc}"
    state["relay"] = {"sig": sig, "at": iso(now)}
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="what each camera has seen")
    ap.add_argument("--frame", help="detect on a saved JPEG instead of fetching")
    ap.add_argument("--camera", help="camera id, with --frame")
    ap.add_argument("--view", help="view id within the camera, with --frame")
    ap.add_argument("--force", action="store_true", help="ignore receiving hours")
    args = ap.parse_args()

    cameras = load_cameras()
    state = read_json(STATE, {"cameras": {}, "errors": []})

    if args.frame:
        cam = next((c for c in cameras if c["id"] == args.camera), cameras[0])
        views = views_of(cam)
        cam = next((v for v in views if v["view"] == (args.view or "")), views[0])
        jpeg = pathlib.Path(args.frame).read_bytes()
        counted = count_regions(detect(jpeg), cam)
        for b in counted["boxes"]:
            print(f"  {b['cls']:6s} {b['conf']:.2f}  h={b['y2'] - b['y1']:4.0f}px  at {b['where'] or 'elsewhere'}")
        print(f"{counted['line']} in line; " +
              ", ".join(f"{k} {'busy' if v else 'open'}" for k, v in counted["pits"].items()))
        out = pathlib.Path(args.frame).with_name(pathlib.Path(args.frame).stem + "-counted.jpg")
        annotate(jpeg, counted, cam, out)
        print(f"annotated copy: {out}")
        return

    if args.report:
        report(cameras, state)
        return

    print(f"watching {len(cameras)} camera(s)...")
    watch_once(cameras, state, force=args.force)
    pushed = push(cameras, state)
    if pushed:
        print(f"  relay: {pushed}")
    write_private(STATE, state, separators=(",", ":"))


if __name__ == "__main__":
    main()
