"""Look at what is actually in your John Deere Operations Center account.

Read-only. Nothing here writes to the repo, publishes anything, or sends your
data anywhere - it prints a summary to the terminal so we can see what exists
before designing around it.

    python dev/jd_explore.py

Credentials
-----------
Put your app's client id and secret in a file OUTSIDE this repo, because this
repo is public:

    %USERPROFILE%\\.grain-map-secrets\\johndeere.json

    {
      "client_id": "...",
      "client_secret": "...",
      "redirect_uri": "http://localhost:9090/callback"
    }

Register that exact redirect URI in your app at developer.deere.com. The
client secret never leaves this machine; the sign-in happens in your browser.

What it prints
--------------
  * your organizations, and whether each is actually connected
  * machines, with the age of each one's last reported position
  * fields, with boundary point counts

The position ages are the number that decides whether "live truck tracking" is
achievable or whether Deere only refreshes every several minutes.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import http.server
import json
import os
import pathlib
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

SECRETS = pathlib.Path.home() / ".grain-map-secrets" / "johndeere.json"
TOKEN_CACHE = pathlib.Path.home() / ".grain-map-secrets" / "johndeere-token.json"

AUTHORIZE = "https://signin.johndeere.com/oauth2/aus78tnlaysMraFhC1t7/v1/authorize"
TOKEN = "https://signin.johndeere.com/oauth2/aus78tnlaysMraFhC1t7/v1/token"
API = "https://partnerapi.deere.com"

# Equipment for the trucks, ag for the fields, offline_access so the farm PC
# can refresh without a browser once this has been done by hand.
SCOPES = "ag1 ag2 eq1 eq2 org1 org2 offline_access"

ACCEPT = "application/vnd.deere.axiom.v3+json"


def load_config() -> dict:
    if not SECRETS.exists():
        sys.exit(
            f"No John Deere credentials found.\n"
            f"  Create {SECRETS} containing:\n"
            '    {"client_id": "...", "client_secret": "...",\n'
            '     "redirect_uri": "http://localhost:9090/callback"}\n'
            "  and register that redirect URI on your app at developer.deere.com."
        )
    # utf-8-sig: Notepad and PowerShell's Out-File both write a BOM, and the
    # plain utf-8 reader chokes on it with a confusing error.
    cfg = json.loads(SECRETS.read_text(encoding="utf-8-sig"))
    for key in ("client_id", "redirect_uri"):
        if not cfg.get(key):
            sys.exit(f"{SECRETS} is missing {key!r}")
    if str(cfg["client_id"]).startswith("PASTE_"):
        sys.exit(
            f"{SECRETS} still has the placeholder in it.\n"
            "  Put your real client id and secret in that file first -\n"
            "  get them from your app at developer.deere.com."
        )
    return cfg


def _post_form(url: str, data: dict, auth: tuple[str, str] | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    if auth:
        raw = f"{auth[0]}:{auth[1]}".encode()
        req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode())
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def sign_in(cfg: dict) -> dict:
    """Authorization-code flow with PKCE, catching the redirect on localhost."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    parsed = urllib.parse.urlparse(cfg["redirect_uri"])
    # An unparseable redirect_uri used to surface as a TypeError from deep
    # inside socketserver, which says nothing about the actual problem.
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        sys.exit(
            f"redirect_uri in {SECRETS} is not a URL:\n"
            f"  {cfg['redirect_uri']!r}\n"
            "  It should be exactly:  http://localhost:9090/callback\n"
            "  Fix it with:  python dev/jd_setup.py"
        )
    caught: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(path.query)
            # Browsers fetch /favicon.ico unprompted. Answering it must not
            # count as the callback, or the real one arrives to a dead socket.
            if "code" not in qs and "error" not in qs:
                self.send_response(204)
                self.end_headers()
                return
            caught.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>Signed in. You can close this tab.</h3>")

        def log_message(self, *_args):
            pass

    try:
        server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    except OSError as exc:
        sys.exit(
            f"Could not listen on {cfg['redirect_uri']}: {exc}\n"
            "  Something else may already be using that port."
        )
    # serve_forever rather than a single handle_request: the callback is not
    # guaranteed to be the first request that arrives.
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = AUTHORIZE + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("Opening your browser to sign in to John Deere...")
    print(f"  if it does not open, paste this in yourself:\n  {url}\n")
    webbrowser.open(url)

    print("Waiting for the browser to come back (5 minutes)...")
    for _ in range(600):
        if caught:
            break
        threading.Event().wait(0.5)
    server.shutdown()

    if not caught:
        sys.exit(
            "Timed out waiting for the browser redirect.\n"
            "  If Deere showed an error page, the redirect URI on your app at\n"
            f"  developer.deere.com must match {cfg['redirect_uri']} exactly."
        )
    if "error" in caught:
        sys.exit(f"Deere refused the sign-in: {caught.get('error')} "
                 f"- {caught.get('error_description', '')}")
    if caught.get("state") != state:
        sys.exit("state mismatch - aborting rather than trusting that redirect")
    if "code" not in caught:
        sys.exit(f"sign-in failed: {caught}")
    print("Got the callback, exchanging it for a token...")

    payload = {
        "grant_type": "authorization_code",
        "code": caught["code"],
        "redirect_uri": cfg["redirect_uri"],
        "code_verifier": verifier,
    }

    # Try with the secret, then without. Deere's OAuth metadata lists "none"
    # among the accepted token endpoint auth methods and advertises PKCE, so a
    # public client works - which means a wrong or mistyped secret should not
    # be the thing that blocks sign-in when PKCE alone would do.
    attempts = []
    if cfg.get("client_secret"):
        attempts.append(("client secret", dict(payload),
                         (cfg["client_id"], cfg["client_secret"])))
    attempts.append(("PKCE only", {**payload, "client_id": cfg["client_id"]}, None))

    last_error = None
    for label, body, auth in attempts:
        try:
            token = _post_form(TOKEN, body, auth)
            if len(attempts) > 1 and label == "PKCE only":
                print("  (the client secret was rejected; signed in with PKCE instead -"
                      " re-check the secret in developer.deere.com)")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            last_error = f"{label}: HTTP {exc.code} {detail}"
            print(f"  {label} failed, trying the next method...")
    else:
        sys.exit(f"Could not exchange the code for a token.\n  {last_error}")
    TOKEN_CACHE.write_text(json.dumps(token, indent=1), encoding="utf-8")
    try:
        os.chmod(TOKEN_CACHE, 0o600)
    except OSError:
        pass
    return token


def api(token: str, path: str) -> dict:
    url = path if path.startswith("http") else API + path
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", ACCEPT)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def age(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        t = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    mins = (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() / 60
    if mins < 90:
        return f"{mins:.0f} min ago"
    if mins < 60 * 48:
        return f"{mins / 60:.1f} h ago"
    return f"{mins / 1440:.0f} d ago"


def main() -> None:
    cfg = load_config()
    token = sign_in(cfg)["access_token"]

    orgs = api(token, "/platform/organizations").get("values", [])
    print(f"\n{len(orgs)} organization(s):")
    connect_needed = []
    for org in orgs:
        links = {l.get("rel"): l.get("uri") for l in org.get("links", [])}
        state = "connected" if "connections" not in links else "NOT CONNECTED"
        print(f"  {org.get('name')}  id={org.get('id')}  [{state}]")
        if "connections" in links:
            connect_needed.append(links["connections"])

    if connect_needed:
        print("\nDeere needs you to grant this app access to the organization.")
        print("Open this once, approve, then re-run:")
        for uri in connect_needed:
            print("  " + uri)
        return

    for org in orgs:
        oid = org.get("id")
        print(f"\n=== {org.get('name')} ===")

        try:
            machines = api(token, f"/platform/organizations/{oid}/machines").get("values", [])
        except Exception as exc:  # noqa: BLE001
            machines = []
            print(f"  machines: {type(exc).__name__} {exc}")
        print(f"  {len(machines)} machine(s)")
        for m in machines[:40]:
            print(f"     {str(m.get('name'))[:30]:32s} "
                  f"{str(m.get('category') or m.get('type'))[:14]:16s} "
                  f"vin={m.get('vin') or '-'}")

        try:
            fields = api(token, f"/platform/organizations/{oid}/fields").get("values", [])
        except Exception as exc:  # noqa: BLE001
            fields = []
            print(f"  fields: {type(exc).__name__} {exc}")
        print(f"  {len(fields)} field(s)")
        for f in fields[:15]:
            print(f"     {str(f.get('name'))[:34]:36s} id={f.get('id')}")
        if len(fields) > 15:
            print(f"     ... and {len(fields) - 15} more")


if __name__ == "__main__":
    main()
