"""Read the E4 / Waypoint soil report PDFs and check the transcribed sheet.

    python dev/jd_rx_soil_pdf.py "<soil-import dir>"
    python dev/jd_rx_soil_pdf.py "<soil-import dir>" --page-dump 1

The 2026 soil data reached the prescription builder through a spreadsheet
someone transcribed from these reports - one row of field averages per
report - and those numbers now set fertilizer rates on 53 South fields
that have no other soil data. The reports themselves are in the archive:
two PDFs, 4,081 pages, North and South.

They are not scans. Every page carries a text layer with the lab's own
per-sample table, so the transcription can be checked against the source
rather than by eye. This reads every report, averages its samples the way
the spreadsheet says it did, and prints what disagrees.

Layout, per page (landscape, one row per sample):

    Sample Id | Organic Matter % | ENR | CEC | pH | Buffer pH | P | K |
    Ca | Mg | S | B | Cu | Fe | Mn | Zn | Na        and, on the right,
    the calculated cation saturations %K %Ca %Mg %H %Na

Values are placed under their column, and a test the lab did not run
leaves its cell EMPTY - buffer pH is only run on acid samples. So values
are read by their x position against the header, never by counting them
in order, or one missing buffer pH would shift a whole row.

Writes ~/.grain-map-secrets/soil-reports.json: every sample of every
report, which is finer than the spreadsheet and the only per-sample copy
of the 2026 data we have. Still no coordinates - the reports do not carry
them - so this cannot turn a composite into a grid.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys
import warnings

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_common import SECRETS, now_iso, write_private  # noqa: E402

OUTPUT = SECRETS / "soil-reports.json"
PDF_DIR = "E4 (Tony Sents) 2026 Soil Sample Reports"
SHEET = ("2026 Soil Data - Claude Transcribed Excel Version/"
         "ALL 2026 - Waypoint Soil Sample Results.xlsx")

# Header label -> the key this column gets. The spreadsheet's own column
# names are in the comment beside each.
COLUMNS = {
    "Organic": "om",        # OM%
    "ENR": "enr",           # ENR
    "CEC": "cec",           # CEC_meq_100g
    "pH": "ph",             # pH
    "Buffer": "bph",        # Buffer_pH
    "P": "p", "K": "k", "Ca": "ca", "Mg": "mg", "S": "s", "B": "b",
    "Cu": "cu", "Fe": "fe", "Mn": "mn", "Zn": "zn", "Na": "na",
}
# Spreadsheet column -> report key.
SHEET_COLS = {
    "OM%": "om", "ENR": "enr", "CEC_meq_100g": "cec", "pH": "ph",
    "Buffer_pH": "bph", "P_ppm": "p", "K_ppm": "k", "Ca_ppm": "ca",
    "Mg_ppm": "mg", "S_ppm": "s", "B_ppm": "b", "Cu_ppm": "cu",
    "Fe_ppm": "fe", "Mn_ppm": "mn", "Zn_ppm": "zn", "Na_ppm": "na",
    "pctK": "bs_k", "pctCa": "bs_ca", "pctMg": "bs_mg", "pctH": "bs_h",
    "pctNa": "bs_na",
}
NUM = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$")


def to_float(text: str) -> float:
    """A number off the page. Thousands separators appear in the
    average row and in every map-page range, so a plain float() on
    "2,970.3" silently reads 2 and 970.3 as two numbers."""
    return float(text.replace(",", ""))


def header_columns(words: list[dict]) -> dict:
    """{key: x-centre} for this page's table, read off its own header.

    Both the pH column and the buffer pH column are labelled "pH", so the
    buffer one is taken from the word "Buffer" above it and the plain one
    is whichever "pH" is not underneath it."""
    head = [w for w in words if 170 <= w["top"] <= 205]
    cols: dict = {}
    buffer_x = None
    for w in head:
        if w["text"] == "Buffer":
            buffer_x = (w["x0"] + w["x1"]) / 2
    for w in head:
        t = w["text"]
        key = COLUMNS.get(t)
        if not key:
            continue
        x = (w["x0"] + w["x1"]) / 2
        if t == "pH":
            if buffer_x is not None and abs(x - buffer_x) < 12:
                key = "bph"
            else:
                key = "ph"
        cols.setdefault(key, x)
    if buffer_x is not None:
        cols["bph"] = buffer_x
    return cols


def page_header_text(words: list[dict]) -> str:
    """The page's words as text, read in rows.

    Sorting by raw top would interleave the columns of the header block:
    "Report No:" sits at top 86.4 and its value at 86.0, so a plain sort
    puts the value before the label and the report number comes out as
    whatever word follows. Rows are banded first."""
    rows: dict = collections.defaultdict(list)
    for w in words:
        rows[round(w["top"] / 4)].append(w)
    out = []
    for band in sorted(rows):
        out += [w["text"] for w in sorted(rows[band], key=lambda w: w["x0"])]
    return " ".join(out)


# A report number, e.g. 25-325-1137(2): year, day of the year, number,
# and a suffix when the lab reissued it.
REPORT_NO = re.compile(r"^\d\d-\d{1,3}-\d{3,5}(\(\d+\))?$")


def page_report(words: list[dict]) -> dict | None:
    """Report number, field and grower off the page header.

    The number is found by its own shape, not by reading rightwards from
    the words "Report No:". The label and its value are a fraction of a
    point apart vertically, so any row banding can put them in different
    rows and hand back the first word of the next block instead."""
    hits = [w["text"] for w in words if REPORT_NO.match(w["text"])]
    if not hits:
        return None
    line = page_header_text(words)
    out = {"report": hits[0]}
    m = re.search(r"Field Id:\s*(\d+)\s+(\S+)", line)
    if m:
        out["field_id"], out["field"] = m.group(1), m.group(2)
    m = re.search(r"Grower\s*:\s*(Studer \S+)", line)
    if m:
        out["grower"] = m.group(1)
    m = re.search(r"Date Received\s*:\s*(\d\d/\d\d/\d{4})", line)
    if m:
        out["received"] = m.group(1)
    return out


def saturation_columns(words: list[dict]) -> dict:
    """{key: x-centre} for the %K %Ca %Mg %H %Na block on the right."""
    head = {"%K": "bs_k", "%Ca": "bs_ca", "%Mg": "bs_mg", "%H": "bs_h",
            "%Na": "bs_na"}
    out = {}
    for w in words:
        if w["top"] < 205 and w["text"] in head:
            out[head[w["text"]]] = (w["x0"] + w["x1"]) / 2
    return out


def split_merged(tokens: list[str]) -> list[tuple[str, str]]:
    """Split "1316102" into row 131 and sample 6102.

    On the later pages of a long report the row number and the lab's
    sample id are drawn with no gap between them, so they come back as one
    word and the row is missed entirely - which is how 88 of Hansen473's
    188 samples went missing. Both numbers count up by one per row, so the
    split is the one place where that holds for the whole page."""
    if len(tokens) < 2:
        return []
    a, b = tokens[0], tokens[1]
    if len(a) != len(b):
        return []
    for k in range(1, len(a)):
        try:
            if int(a[:k]) + 1 == int(b[:k]) and int(a[k:]) + 1 == int(b[k:]):
                pass
            else:
                continue
        except ValueError:
            continue
        out, ok = [], True
        for t in tokens:
            if len(t) != len(a):
                ok = False
                break
            out.append((t[:k], t[k:]))
        if ok:
            return out
    return []


def row_starts(by_top: dict) -> list:
    """(top, row number, sample id) for each sample row on the page.

    The row number and the lab's sample id are separate words until the row
    number reaches three digits, at which point they touch and come back as
    one word - and a page can hold both kinds, row 111 split and row 112
    merged. Rows that are already split anchor the pair, and every merged
    token is then split at the place that continues the count. Without an
    anchor the page is split on its own internal consistency instead."""
    groups = []
    for top in sorted(by_top):
        row = sorted(by_top[top], key=lambda w: w["x0"])
        groups.append((top, [w["text"] for w in row]))

    anchor = None                      # (position, row number, sample id)
    for i, (_top, texts) in enumerate(groups):
        if len(texts) >= 2 and texts[0].isdigit() and texts[1].isdigit():
            anchor = (i, int(texts[0]), int(texts[1]))
            break

    out = []
    merged_only = []
    for i, (top, texts) in enumerate(groups):
        if len(texts) >= 2:
            out.append((top, texts[0], texts[1]))
            continue
        token = texts[0]
        if anchor:
            pos, idx0, sid0 = anchor
            idx, sid = idx0 + (i - pos), sid0 + (i - pos)
            if token == f"{idx}{sid}":
                out.append((top, str(idx), str(sid)))
                continue
        merged_only.append((i, top, token))

    if merged_only and len(merged_only) == len(groups):
        pairs = split_merged([t for _i, _top, t in merged_only])
        if pairs:
            return [(top, idx, sid)
                    for (_i, top, _t), (idx, sid) in zip(merged_only, pairs)]
        return [(top, "", t) for _i, top, t in merged_only]
    for _i, top, token in merged_only:
        out.append((top, "", token))
    out.sort()
    return out


# Below this the page is the lab's confidentiality notice, not data.
FOOTER_TOP = 580
# A sample's values sit about 15 points below its row start and the next
# row starts 36 below, but the report's own "Average :" row sits only 34
# below the last sample. Keep the window tight enough to exclude it.
ROW_HEIGHT = 20


def page_samples(words: list[dict], cols: dict) -> list[dict]:
    """One dict per sample row on the page."""
    if "p" not in cols or "k" not in cols:
        return []
    sat_cols = saturation_columns(words)
    # Whatever the spacing, nothing at or below the "Average :" row belongs
    # to a sample.
    avg_row = [w["top"] for w in words if w["text"] == "Average" and w["x0"] < 45]
    limit = min(avg_row) if avg_row else FOOTER_TOP
    # The row number and the lab's sample id sit at the far left.
    heads = [w for w in words if w["x0"] < 45 and NUM.match(w["text"])
             and 205 < w["top"] < limit]
    by_top: dict = collections.defaultdict(list)
    for w in heads:
        by_top[round(w["top"] / 4) * 4].append(w)
    starts = row_starts(by_top)
    if not starts:
        return []

    out = []
    for i, (top, idx, sample_id) in enumerate(starts):
        nxt = starts[i + 1][0] if i + 1 < len(starts) else limit + 6
        nxt = min(nxt, top + ROW_HEIGHT + 6, limit + 6)
        rec = {"n": int(idx) if idx.isdigit() else None, "sample": sample_id}
        for w in words:
            if not (top - 6 <= w["top"] < nxt - 6):
                continue
            if w["top"] >= limit or not NUM.match(w["text"]):
                continue
            x = (w["x0"] + w["x1"]) / 2
            if x < 45:
                continue
            table = sat_cols if x > 600 else cols
            key, best = None, 99.0
            for k, cx in table.items():
                d = abs(x - cx)
                if d < best:
                    key, best = k, d
            if key and best <= 18:
                rec[key] = to_float(w["text"])
        out.append(rec)
    return out


def page_average(words: list[dict], cols: dict) -> dict:
    """The report's own "Average :" row, printed under the last samples.

    Worth having: it is the lab's arithmetic on the lab's numbers, so the
    spreadsheet can be checked against it without trusting this parser's
    averaging at all."""
    row = [w for w in words if w["text"] == "Average" and w["x0"] < 45]
    if not row:
        return {}
    top = row[0]["top"]
    sat = saturation_columns(words)
    out: dict = {}
    for w in words:
        if not (top - 5 <= w["top"] <= top + 12):
            continue
        if not NUM.match(w["text"]):
            continue
        x = (w["x0"] + w["x1"]) / 2
        if x < 45:
            continue
        table = sat if x > 600 else cols
        key, best = None, 99.0
        for k, cx in table.items():
            d = abs(x - cx)
            if d < best:
                key, best = k, d
        if key and best <= 18:
            out[key] = to_float(w["text"])
    return out


# The E4 map pages (a picture of the field per nutrient) carry things the
# analysis pages do not: the field's FULL name, its county, its acres, its
# centroid, the crops, and E4's own min / average / max for that nutrient.
# The spreadsheet's names are cut at 14 characters, so these pages are how
# a row like "Richrdsn264Cha" gets its real name and a location.
MAP_CODES = {
    "OM": "om", "CEC": "cec", "PH": "ph", "BPH": "bph",
    "MEHLICHP_COLOR": "p", "K": "k", "CA": "ca", "MG": "mg", "S": "s",
    "B": "b", "Cu": "cu", "Mn": "mn", "Zn": "zn",
    "PERCENTK": "bs_k", "PERCENTCA": "bs_ca", "PERCENTMG": "bs_mg",
    "PERCENTH": "bs_h",
}
NUMS = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")


def page_map(text: str) -> dict | None:
    """A nutrient map page: the field it belongs to and E4's own summary."""
    m = re.search(r"(\d+) sample results for (\S+)", text)
    if not m:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = {"code": m.group(2), "key": MAP_CODES.get(m.group(2)),
           "n": int(m.group(1))}
    for ln in lines[:6]:
        p1 = re.search(r"/\s*(\d{4,6})\s+(\S+)\s*$", ln)
        if p1 and "grower" not in out:
            parts = [x.strip() for x in ln.split("/")]
            out["field_id"], out["field"] = p1.group(1), p1.group(2)
            if len(parts) >= 3:
                out["grower"], out["county"] = parts[1], parts[2]
        p2 = re.match(r"([\d.]+)\s+Acres", ln)
        if p2:
            out["acres"] = float(p2.group(1))
            c = re.search(r"(\d{4}):\s*([A-Za-z ]+),\s*(\d{4}):\s*([A-Za-z ]+?)\s*/", ln)
            if c:
                out["crops"] = {c.group(1): c.group(2).strip(),
                                c.group(3): c.group(4).strip()}
            d = re.search(r"Samples On:\s*(\d+/\d+/\d{4})", ln)
            if d:
                out["sampled"] = d.group(1)
        p3 = re.search(r"Lat/Lng:\s*(-?[\d.]+)\s*/\s*(-?[\d.]+)", ln)
        if p3:
            out["lat"], out["lon"] = float(p3.group(1)), float(p3.group(2))
    for i, ln in enumerate(lines):
        if ln.startswith("Min Max Acres") and i + 1 < len(lines):
            vals = NUMS.findall(lines[i + 1])
            if len(vals) >= 6:
                out["min"], out["avg"], out["max"] = (to_float(v) for v in vals[-3:])
            break
    return out


def read_pdfs(root: pathlib.Path, dump: int | None = None) -> tuple[dict, dict]:
    """Every report's samples, and every field's map-page summary.

    Two kinds of page are worth reading. The lab's analysis pages hold the
    per-sample table and the report number. E4's map pages hold one
    picture per nutrient, and around it the field's full name, county,
    acres, centroid, crops and E4's own min/average/max."""
    import pdfplumber
    reports: dict = {}
    fields: dict = {}
    for pdf_path in sorted((root / PDF_DIR).glob("*.pdf")):
        org = "North" if "North" in pdf_path.name else "South"
        print(f"  {pdf_path.name}")
        with pdfplumber.open(str(pdf_path)) as pdf:
            for pno, page in enumerate(pdf.pages):
                if dump is not None and pno == dump:
                    for w in sorted(page.extract_words(),
                                    key=lambda w: (w["top"], w["x0"])):
                        print(f"    top={w['top']:7.1f} x={w['x0']:7.1f} {w['text']!r}")
                    return {}, {}
                if (pno + 1) % 500 == 0:
                    print(f"    page {pno + 1} of {len(pdf.pages)}, "
                          f"{len(reports)} reports, {len(fields)} fields")
                text = page.extract_text() or ""
                mp = page_map(text)
                if mp is None and "SOIL ANALYSIS" not in text:
                    # Nothing here to read. pdfplumber keeps every page it
                    # has parsed, and 4,081 of them will not fit in memory.
                    page.flush_cache()
                    page.get_textmap.cache_clear()
                    continue
                if mp:
                    fid = mp.get("field_id")
                    if not fid:
                        page.flush_cache()
                        page.get_textmap.cache_clear()
                        continue
                    f = fields.setdefault(fid, {"org": org, "field_id": fid,
                                                "e4": {}})
                    for k in ("field", "grower", "county", "acres", "lat",
                              "lon", "sampled", "crops"):
                        if k in mp:
                            f.setdefault(k, mp[k])
                    if mp.get("key") and "avg" in mp:
                        f["e4"][mp["key"]] = {"min": mp["min"], "avg": mp["avg"],
                                              "max": mp["max"], "n": mp["n"]}
                    page.flush_cache()
                    page.get_textmap.cache_clear()
                    continue
                words = page.extract_words()
                meta = page_report(words)
                if not meta:
                    continue
                cols = header_columns(words)
                rows = page_samples(words, cols)
                printed = page_average(words, cols)
                page.flush_cache()
                page.get_textmap.cache_clear()
                if not rows:
                    continue
                r = reports.setdefault(meta["report"], {
                    "report": meta["report"], "org": org,
                    "field": meta.get("field"), "field_id": meta.get("field_id"),
                    "grower": meta.get("grower"), "received": meta.get("received"),
                    "pages": [], "samples": []})
                r["pages"].append(pno + 1)
                have = {s["sample"] for s in r["samples"]}
                r["samples"] += [s for s in rows if s["sample"] not in have]
                if printed:
                    r["printed_avg"] = printed
    return reports, fields


def averages(samples: list[dict]) -> dict:
    tot: dict = {}
    for s in samples:
        for k, v in s.items():
            if k in ("n", "sample") or not isinstance(v, (int, float)):
                continue
            a, n = tot.get(k, (0.0, 0))
            tot[k] = (a + v, n + 1)
    return {k: (a / n, n) for k, (a, n) in tot.items() if n}


def read_sheet(root: pathlib.Path) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(root / SHEET, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = [str(c).strip() if c is not None else "" for c in rows[0]]
    idx = {h: i for i, h in enumerate(head)}
    out = {}
    for r in rows[1:]:
        rep = str(r[idx["Report_No"]]).strip()
        rec = {"field": str(r[idx["FieldName"]]).strip(),
               "grower": str(r[idx["Grower"]]).strip(),
               "field_id": str(r[idx["Field_Id"]]).strip()}
        for col, key in SHEET_COLS.items():
            if col in idx and isinstance(r[idx[col]], (int, float)):
                rec[key] = float(r[idx[col]])
        out[rep] = rec
    wb.close()
    return out


def diff_table(title: str, pairs: list, tol: float, limit: int = 40) -> None:
    """pairs: (report, key, source value, sheet value, note)."""
    bad = [(abs(b - a) / (abs(a) or 1), rep, key, a, b, note)
           for rep, key, a, b, note in pairs if abs(b - a) / (abs(a) or 1) > tol]
    bad.sort(reverse=True)
    print("")
    print(f"{title}, off by more than {tol:.0%}:")
    for d, rep, key, a, b, note in bad[:limit]:
        print(f"  {rep:18s} {key:6s} report {a:10.3f}   sheet {b:10.3f}   "
              f"{d:6.1%} {note}")
    counts = collections.Counter(key for _d, _r, key, _a, _b, _n in bad)
    print(f"  {len(pairs)} compared, {len(bad)} off by more than {tol:.0%}")
    if counts:
        print("  by column: " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))


def compare(reports: dict, fields: dict, sheet: dict, tol: float) -> None:
    """Three ways of saying what a field tested, side by side.

    The spreadsheet is one row of averages per report. Each report prints
    its own average under its samples, which is the lab's arithmetic on the
    lab's numbers and so the fairest thing to hold the spreadsheet against.
    Re-averaging the samples here is a check on that, and E4's map pages
    are a third opinion from the people who drew the maps."""
    only_sheet = sorted(set(sheet) - set(reports))
    only_pdf = sorted(set(reports) - set(sheet))
    n_samples = sum(len(r["samples"]) for r in reports.values())
    print("")
    print(f"reports in the PDFs: {len(reports)} ({n_samples:,} samples)    "
          f"rows in the sheet: {len(sheet)}    "
          f"fields with map pages: {len(fields)}")

    if only_sheet:
        print("")
        print(f"{len(only_sheet)} sheet rows with no report in the PDFs:")
        for rep in only_sheet:
            print(f"  {rep:18s} {sheet[rep]['field']:16s} {sheet[rep]['grower']}")
    if only_pdf:
        print("")
        print(f"{len(only_pdf)} reports in the PDFs the sheet does not have "
              f"(fields whose 2026 soil test never reached the builder):")
        for rep in only_pdf:
            r = reports[rep]
            f = fields.get(r.get("field_id") or "") or {}
            print(f"  {rep:18s} {str(f.get('field') or r['field']):30s} "
                  f"{r['grower']:20s} {len(r['samples']):4d} samples  "
                  f"{r.get('received') or ''}")

    both = sorted(set(reports) & set(sheet))

    print("")
    print("sample counts, parsed vs E4's own count:")
    bad_n = 0
    for rep in both:
        r = reports[rep]
        f = fields.get(r.get("field_id") or "")
        e4n = max((v["n"] for v in f["e4"].values()), default=None) if f else None
        if e4n and e4n != len(r["samples"]):
            bad_n += 1
            print(f"  {rep:18s} {str(r['field']):16s} parsed {len(r['samples'])}"
                  f", E4 says {e4n}")
    print(f"  {bad_n} disagree" if bad_n else "  all agree")

    printed, resampled, e4pairs = [], [], []
    for rep in both:
        r, s_row = reports[rep], sheet[rep]
        avg = averages(r["samples"])
        pa = r.get("printed_avg") or {}
        f = fields.get(r.get("field_id") or "") or {}
        for key, v in sorted(pa.items()):
            if key in s_row:
                printed.append((rep, key, v, s_row[key], ""))
        for key, (mean, n) in sorted(avg.items()):
            if key in s_row:
                note = f"(n={n}/{len(r['samples'])})"
                resampled.append((rep, key, mean, s_row[key], note))
        for key, v in sorted(f.get("e4", {}).items()):
            if key in pa:
                e4pairs.append((rep, key, pa[key], v["avg"], "(E4 map)"))

    diff_table("SHEET vs the report's own printed average", printed, tol)
    diff_table("SHEET vs re-averaging the lab's samples here", resampled, tol)
    diff_table("the report's printed average vs E4's map pages", e4pairs, tol, 12)

    print("")
    print("field names the sheet had to cut, in full, with their centroid:")
    shown = 0
    for rep in both:
        r, s_row = reports[rep], sheet[rep]
        f = fields.get(r.get("field_id") or "")
        if not f or not f.get("field") or f["field"] == s_row["field"]:
            continue
        shown += 1
        print(f"  {s_row['field']:16s} -> {f['field']:30s} "
              f"{str(f.get('county')):14s} {f.get('acres') or 0:7.1f} ac  "
              f"{f.get('lat') or 0:.5f},{f.get('lon') or 0:.5f}")
    print(f"  {shown} of {len(both)} reports")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", type=pathlib.Path)
    ap.add_argument("--tol", type=float, default=0.02,
                    help="relative difference worth printing (default 2%%)")
    ap.add_argument("--page-dump", type=int, default=None,
                    help="print one page's words and stop, for layout work")
    args = ap.parse_args()
    warnings.filterwarnings("ignore")
    root = args.root.expanduser()

    reports, fields = read_pdfs(root, args.page_dump)
    if args.page_dump is not None:
        return
    n_samples = sum(len(r["samples"]) for r in reports.values())
    print(f"read {len(reports)} reports, {n_samples:,} samples, "
          f"{len(fields)} fields with map pages")
    write_private(OUTPUT, {"generated_at": now_iso(), "reports": reports,
                           "fields": fields}, separators=(",", ":"))
    print(f"wrote {OUTPUT}")
    compare(reports, fields, read_sheet(root), args.tol)


if __name__ == "__main__":
    main()
