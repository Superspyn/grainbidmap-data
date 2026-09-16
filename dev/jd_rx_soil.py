"""Lift the soil-test data out of the chat-built prescription page.

    python dev/jd_rx_soil.py "C:/Users/acces/Downloads/prescription_builder (53).html"

The first prescription builder was made in a claude.ai chat, and the lab
results (Waypoint Analytical and Midwest Labs grids, 2014-2025) exist only
as JSON baked into that HTML. This pulls them back out into

    ~/.grain-map-secrets/soil-samples.json

so the page can be rebuilt from data rather than from a previous page.
Per field: the county, and one entry per sampling event with the field
averages (from the page's FI table) and the grid points (from its FP table),
joined on the sample date.

Private data, outside the public repo on purpose.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, now_iso, write_private  # noqa: E402

OUTPUT = SECRETS / "soil-samples.json"


def _json_after(line: str, prefix: str, suffix: str) -> dict:
    body = line[len(prefix):].rstrip()
    if suffix and body.endswith(suffix):
        body = body[: -len(suffix)]
    return json.loads(body)


def extract(html: str) -> dict:
    """The FI (averages) and FP (points) tables, merged per field and date."""
    averages: dict = {}
    points: dict = {}
    for line in html.split("\n"):
        if line.startswith("const FI="):
            averages = _json_after(line, "const FI=", ";")
        elif line.startswith("Object.assign(FP,"):
            points.update(_json_after(line, "Object.assign(FP,", ");"))
    if not averages:
        raise SystemExit("no 'const FI=' line - is this the prescription builder?")

    fields: dict = {}
    for name, fi in averages.items():
        by_date = {s["d"]: s for s in points.get(name, {}).get("sets", [])}
        sets = []
        for s in fi["sets"]:
            avg = {k: v for k, v in s.items() if k not in ("d", "n", "lab")}
            pts = by_date.get(s["d"], {}).get("pts", [])
            sets.append({"d": s["d"], "n": s.get("n"), "lab": s.get("lab"),
                         "avg": avg, "pts": pts})
        # Any point set the averages table did not know about.
        known = {s["d"] for s in sets}
        for d, s in by_date.items():
            if d not in known:
                sets.append({"d": d, "n": len(s["pts"]), "lab": s.get("lab"),
                             "avg": {}, "pts": s["pts"]})
        sets.sort(key=lambda s: s["d"])
        fields[name] = {"county": fi.get("county"), "sets": sets}
    return fields


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    src = pathlib.Path(sys.argv[1])
    fields = extract(src.read_text(encoding="utf-8"))
    n_sets = sum(len(f["sets"]) for f in fields.values())
    n_pts = sum(len(s["pts"]) for f in fields.values() for s in f["sets"])
    write_private(OUTPUT, {"generated_at": now_iso(), "source": src.name,
                           "fields": fields}, separators=(",", ":"))
    print(f"wrote {OUTPUT}: {len(fields)} fields, {n_sets} sampling events, "
          f"{n_pts:,} grid points")


if __name__ == "__main__":
    main()
