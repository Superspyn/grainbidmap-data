"""Set up the truck alert texts, without hand-editing JSON.

    python dev/jd_sms_setup.py

Asks for your mobile number, your carrier and a Gmail App Password, and
writes them to

    %USERPROFILE%\\.grain-map-secrets\\sms.json

outside this repo, because this repo is public. The app password is typed
hidden and is never echoed, printed or logged. Re-running keeps whatever you
leave blank, so this is also the way to change your number without retyping
the password.

Alerts go out as ordinary texts through your carrier's mail gateway, which
costs nothing. You need a Gmail **App Password** - a separate 16-character
one, not your account password - from myaccount.google.com/apppasswords,
which requires 2-step verification to be switched on.
"""
from __future__ import annotations

import getpass
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jd_notify import CARRIERS, CONFIG, sms_address  # noqa: E402

SECRETS_DIR = pathlib.Path.home() / ".grain-map-secrets"

# The names worth offering; CARRIERS holds the aliases too.
OFFER = ["verizon", "att", "tmobile", "uscellular", "cricket", "boost",
         "googlefi", "mint", "tracfone", "straight talk"]


def existing() -> dict:
    if not CONFIG.exists():
        return {}
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}


def ask(prompt: str, current: str | None = None) -> str:
    shown = f" [{current}]" if current else ""
    answer = input(f"{prompt}{shown}: ").strip()
    return answer or (current or "")


def looks_like_an_account_password(value: str) -> bool:
    """Catch the Gmail account password typed at the App Password prompt.

    Google's app passwords are sixteen letters, usually shown in four
    groups. An account password is almost always something else, and the
    only symptom of getting it wrong is a bare authentication failure hours
    later when a truck finally stops.
    """
    return len(re.sub(r"\s", "", value)) != 16 or not value.replace(" ", "").isalpha()


def main() -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    cfg = existing()
    print("Truck alert texts - free, through your carrier's mail gateway.\n")

    phone_entry = (cfg.get("to") or [{}])[0]
    if isinstance(phone_entry, str):
        phone_entry = {}
    phone = ask("Your mobile number", phone_entry.get("phone"))
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        sys.exit(f"'{phone}' is not a 10-digit US mobile number.")

    print(f"\nCarriers: {', '.join(OFFER)}")
    carrier = ask("Your carrier", phone_entry.get("carrier")).lower()
    if carrier not in CARRIERS:
        sys.exit(f"'{carrier}' is not one I know. Pick from the list above, "
                 "or say which carrier it is and it can be added.")

    address = sms_address({"phone": digits, "carrier": carrier})
    print(f"  -> texts will be sent to {address}\n")

    gmail = ask("The Gmail address they are sent from", cfg.get("username"))
    if "@" not in gmail:
        sys.exit(f"'{gmail}' is not an email address.")

    print("\nGmail App Password - 16 letters, from myaccount.google.com/apppasswords")
    print("(NOT your normal Gmail password. It will not be shown as you type.)")
    keep = " [press Enter to keep the saved one]" if cfg.get("app_password") else ""
    typed = getpass.getpass(f"App password{keep}: ").strip()
    if typed:
        # Echo the length only, never the value. A silently mis-pasted
        # secret is the hardest kind of failure to diagnose later - and
        # Google shows the password in four groups, so spaces get pasted
        # along with it and must not be counted.
        bare = re.sub(r"\s", "", typed)
        print(f"  (read {len(bare)} characters)")
        if looks_like_an_account_password(typed):
            print("  Warning: that does not look like an App Password, which is"
                  " sixteen letters with no digits or symbols.")
            if input("  Save it anyway? [y/N]: ").strip().lower() != "y":
                sys.exit("Nothing written.")
        # Store it without the display spaces; Gmail accepts either, but one
        # form means one thing to compare against later.
        app_password = bare
    elif cfg.get("app_password"):
        app_password = cfg["app_password"]
    else:
        sys.exit("No app password given, and none saved. Nothing written.")

    quiet = ask("Quiet hours, as start-end in 24h time (blank for 21-6)",
                "-".join(str(h) for h in cfg.get("quiet_hours", [])) or "21-6")
    match = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", quiet)
    if not match:
        sys.exit(f"'{quiet}' is not a range like 21-6.")
    quiet_hours = [int(match.group(1)), int(match.group(2))]

    out = {
        "provider": "email",
        "username": gmail,
        "app_password": app_password,
        "to": [{"phone": digits, "carrier": carrier}],
        "quiet_hours": quiet_hours,
        "max_per_hour": int(cfg.get("max_per_hour") or 6),
    }
    CONFIG.write_text(json.dumps(out, indent=1), encoding="utf-8")
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass

    print(f"\nWrote {CONFIG}")
    print(f"  texts to {address}, quiet {quiet_hours[0]}:00-{quiet_hours[1]}:00, "
          f"at most {out['max_per_hour']} an hour")
    print("\nCheck it without sending anything:")
    print("  python dev/jd_notify.py --test --dry-run")
    print("Then send one for real:")
    print("  python dev/jd_notify.py --test")


if __name__ == "__main__":
    main()
