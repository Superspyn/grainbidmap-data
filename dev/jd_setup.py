"""Write the John Deere credentials file, without hand-editing JSON.

    python dev/jd_setup.py

Asks for the client id and secret and writes them to

    %USERPROFILE%\\.grain-map-secrets\\johndeere.json

outside this repo, because this repo is public. The secret is typed hidden and
is never echoed, printed or logged. Re-running keeps whatever you leave blank,
so this is also the way to change one value without retyping the other.
"""
from __future__ import annotations

import getpass
import json
import os
import pathlib
import sys

SECRETS_DIR = pathlib.Path.home() / ".grain-map-secrets"
PATH = SECRETS_DIR / "johndeere.json"
DEFAULT_REDIRECT = "http://localhost:9090/callback"

PLACEHOLDER = "PASTE_"


def existing() -> dict:
    if not PATH.exists():
        return {}
    try:
        # Notepad and PowerShell both write a BOM; utf-8-sig eats it.
        cfg = json.loads(PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}
    # Treat leftover placeholders as absent rather than as real values.
    return {k: v for k, v in cfg.items()
            if not (isinstance(v, str) and v.startswith(PLACEHOLDER))}


def main() -> None:
    cfg = existing()
    print(f"Writing {PATH}\n")

    have_id = cfg.get("client_id")
    prompt = "Client ID"
    if have_id:
        prompt += f" [keep current, ...{str(have_id)[-4:]}]"
    entered = input(prompt + ": ").strip()
    if entered:
        cfg["client_id"] = entered
    elif not have_id:
        sys.exit("A client id is required.")

    # getpass keeps the secret off the screen and out of any terminal scrollback.
    prompt = "Client secret"
    if cfg.get("client_secret"):
        prompt += " [enter to keep current]"
    entered = getpass.getpass(prompt + ": ").strip()
    if entered:
        cfg["client_secret"] = entered

    current_redirect = cfg.get("redirect_uri") or DEFAULT_REDIRECT
    entered = input(f"Redirect URI [{current_redirect}]: ").strip()
    cfg["redirect_uri"] = entered or current_redirect

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(PATH, 0o600)
    except OSError:
        pass

    print("\nSaved.")
    print(f"  client_id     : ...{str(cfg['client_id'])[-4:]}")
    print(f"  client_secret : {'set' if cfg.get('client_secret') else 'not set'}")
    print(f"  redirect_uri  : {cfg['redirect_uri']}")
    print("\nMake sure that redirect URI is registered on your app at "
          "developer.deere.com,\nthen run:  python dev/jd_explore.py")


if __name__ == "__main__":
    main()
