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
import urllib.parse

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
    # Treat leftover placeholders - and anything previously saved that is
    # clearly not a credential - as absent, so a bad value does not get kept
    # by pressing Enter past it.
    clean = {}
    for k, v in cfg.items():
        if not isinstance(v, str) or v.startswith(PLACEHOLDER):
            continue
        if k in ("client_id", "client_secret") and looks_like_a_command(v):
            continue
        clean[k] = v
    return clean


def looks_like_a_command(value: str) -> bool:
    """Catch a shell command pasted at a credential prompt.

    This happened: the run command went into all three prompts, and the only
    symptom was a bare 400 from Deere with nothing pointing at the cause.
    Credentials from Deere have no spaces; commands and paths do.
    """
    v = value.strip()
    return (
        " " in v
        or v.startswith(("cd ", ".\\", "./", "python", "C:", "c:"))
        or "\\" in v
        or "/" in v
    )


def ask(label: str, current: str | None, hidden: bool) -> str | None:
    """Prompt until the answer looks like a credential, or is left blank."""
    while True:
        prompt = label
        if current:
            prompt += " [enter to keep current]"
        entered = (getpass.getpass if hidden else input)(prompt + ": ").strip()
        if not entered:
            return None
        if looks_like_a_command(entered):
            print(f"  That looks like a command or a path, not a {label.lower()}.")
            print("  Paste the value from your app at developer.deere.com.")
            continue
        return entered


def main() -> None:
    cfg = existing()
    print(f"Writing {PATH}")
    print("Paste the two values from your app at developer.deere.com.")
    print("Do not paste the command you used to start this.\n")

    have_id = cfg.get("client_id")
    entered = ask("Client ID", have_id, hidden=False)
    if entered:
        cfg["client_id"] = entered
    elif not have_id:
        sys.exit("A client id is required.")

    # getpass keeps the secret off the screen and out of any terminal scrollback.
    entered = ask("Client secret", cfg.get("client_secret"), hidden=True)
    if entered:
        cfg["client_secret"] = entered

    current_redirect = cfg.get("redirect_uri") or DEFAULT_REDIRECT
    print("\n(press Enter to accept the default - do not paste a command here)")
    while True:
        entered = input(f"Redirect URI [{current_redirect}]: ").strip()
        candidate = entered or current_redirect
        parsed = urllib.parse.urlparse(candidate)
        # Anything that is not a URL gets rejected on the spot. A pasted shell
        # command saved here once, and it surfaced much later as a TypeError
        # from inside socketserver.
        if parsed.scheme in ("http", "https") and parsed.hostname:
            cfg["redirect_uri"] = candidate
            break
        print(f"  {candidate!r} is not a URL. Press Enter to use "
              f"{DEFAULT_REDIRECT}")
        current_redirect = DEFAULT_REDIRECT

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
