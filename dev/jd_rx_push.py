"""Send prescription shapefiles from the Rx builder into Operations Center.

    python dev/jd_rx_push.py --org 5294 VR_Rx_batch_P2O5_2027.zip
    python dev/jd_rx_push.py --org 5294 VR_Rx_Meyer171Madsn6_P2O5_soybean_2027.zip
    python dev/jd_rx_push.py --org 5294 --list

Takes either one field's shapefile zip (as the VR Rx tab downloads it) or the
batch zip (which holds one such zip per field plus the order summary CSV)
and uploads each field's zip as a PRESCRIPTION file in the organization's
Files, where Operations Center turns it into a prescription you can assign
to a field and a job. --list shows what is already there. --dry-run shows
what would be sent without sending.

NEEDS THE 'files' SCOPE. The token the map scripts use was issued without
it, so first:

    python dev/jd_explore.py --with-files

which signs in again in the browser (the app must have the Files API
enabled on developer.deere.com, or Deere refuses the request). Until then
every call here returns 403.

Two-step upload, per Deere's Files API: POST the file's name and type to
the organization's files collection, which answers 201 with the new file's
URL in the Location header; then PUT the bytes to that URL. The type must
be PRESCRIPTION for Operations Center to treat it as one.
"""
from __future__ import annotations

import argparse
import io
import pathlib
import sys
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_fleet import ACCEPT, API, api_all, refresh_token  # noqa: E402


def _request(token: str, url: str, method: str, body: bytes | None, ctype: str | None):
    if not url.startswith("http"):
        url = API + url
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", ACCEPT)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()[:400]


def upload(token: str, org: str, name: str, data: bytes, dry_run: bool) -> str:
    if dry_run:
        return f"would upload {name} ({len(data) / 1024:.0f} KB)"
    meta = ('{"name": %s, "type": "PRESCRIPTION"}' % _json_str(name)).encode()
    status, headers, body = _request(
        token, f"/platform/organizations/{org}/files", "POST", meta, ACCEPT)
    if status == 403:
        return "403 - the token has no 'files' scope; run dev/jd_explore.py --with-files"
    if status not in (200, 201) or not headers.get("Location"):
        return f"create failed: {status} {body[:200]!r}"
    file_url = headers["Location"]
    status, _h, body = _request(token, file_url, "PUT", data, "application/zip")
    if status not in (200, 201, 204):
        return f"upload failed: {status} {body[:200]!r}"
    return f"uploaded -> {file_url}"


def _json_str(s: str) -> str:
    import json
    return json.dumps(s)


def field_zips(path: pathlib.Path):
    """(name, bytes) for each shapefile zip: the file itself, or, for the
    batch download, every zip inside it."""
    raw = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        inner = [n for n in z.namelist() if n.lower().endswith(".zip")]
        if inner:
            for n in inner:
                yield pathlib.Path(n).name, z.read(n)
            return
    yield path.name, raw


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("zips", nargs="*", type=pathlib.Path)
    ap.add_argument("--org", required=True, help="organization id (North 5294, South 567678)")
    ap.add_argument("--list", action="store_true", help="list the organization's files")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    token = refresh_token()

    if args.list:
        files = api_all(token, f"/platform/organizations/{args.org}/files")
        if not files:
            print("no files listed (a 403 here means the token lacks the files scope)")
        for f in files:
            print(f"  {str(f.get('name'))[:50]:52s} {str(f.get('type')):14s} "
                  f"{f.get('status') or ''} {f.get('modifiedTime') or ''}")
        return

    if not args.zips:
        ap.error("give one or more zips, or --list")
    for path in args.zips:
        for name, data in field_zips(path):
            print(f"{name}: {upload(token, args.org, name, data, args.dry_run)}")


if __name__ == "__main__":
    main()
