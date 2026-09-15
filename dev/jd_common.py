"""What the jd_* modules kept writing separately.

Five copies of "read this JSON out of the secrets directory or fall back",
five of "write it and chmod 600, swallowing OSError", six of the same ISO
timestamp parser, three distance-in-metres functions. They had already
drifted: two readers used utf-8-sig and three did not, so a hand-edited
file that Notepad had given a BOM parsed in one module and crashed another;
one timestamp parser swallowed ValueError and one did not, so a truncated
state file took the alerts down until someone deleted it.

Nothing here knows anything about Deere. It is the floor the others stand
on, and it must stay import-free of them - jd_idle imports jd_fleet lazily
to dodge a cycle, and this is where that would have become one.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import pathlib

SECRETS = pathlib.Path.home() / ".grain-map-secrets"


def read_json(path: pathlib.Path, default):
    """The file's JSON, or `default` if it is missing or unreadable.

    utf-8-sig, because the files in the secrets directory are documented as
    hand-written and Notepad writes a byte-order mark by default."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return default


def write_private(path: pathlib.Path, obj, **dumps) -> None:
    """Write JSON readable by the owner only. The chmod is best-effort - on
    Windows it is a no-op and the directory's ACL does the work."""
    path.write_text(json.dumps(obj, **dumps), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def parse_iso(value) -> _dt.datetime | None:
    """An ISO-8601 timestamp, Deere's trailing Z included, or None. Never
    raises: a bad timestamp in a state file is not a reason to stop."""
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def iso(when: _dt.datetime) -> str:
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def now_iso() -> str:
    return iso(_dt.datetime.now(_dt.timezone.utc))


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Ground distance in a flat local frame. Exact enough for "did the truck
    move" at the tens-of-metres scale it is asked at; at 60 m the error
    against the ellipsoid is under a centimetre."""
    dlat = (lat2 - lat1) * 111_320.0
    dlon = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


def signed_area(points: list[list[float]]) -> float:
    """Shoelace area in square degrees on (lon, lat), so the sign follows the
    shapefile convention: negative is clockwise, an outer ring. Used both to
    label holes when a boundary is read and to wind rings for the page, and
    it must be the same function in both places or the two disagree."""
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        total += lon1 * lat2 - lon2 * lat1
    return total / 2.0
