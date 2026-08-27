"""Shared value parsing for grain bid data.

The upstream feeds are inconsistent about units, so every adapter funnels its
raw values through here to land on one canonical representation:

  * all prices are USD per bushel, as floats
  * all dates are ISO ``YYYY-MM-DD`` strings
  * ``grain`` is one of ``corn`` / ``soybeans``; anything else is dropped
"""

from __future__ import annotations

import datetime as _dt
import re

# CBOT grains quote in eighths notation ("511-6" == 511 and 6/8 cents), even
# though the tradeable tick is a quarter cent.
_TICK_RE = re.compile(r"^\s*(?P<sign>[+-]?)(?P<whole>\d+)-(?P<frac>\d+)\s*$")
# The sign may sit on either side of the dollar sign: "-$0.03" and "$-0.03"
# both occur in these feeds.
_MONEY_RE = re.compile(r"^\s*(?P<pre>[+-]?)\$?\s*(?P<post>[+-]?)(?P<num>[\d,]*\.?\d+)\s*$")

# Futures month letter codes, as they appear in symbols like ZCZ26 / CU26.
MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

CORN = "corn"
SOYBEANS = "soybeans"


def parse_tick(value) -> float | None:
    """Parse a futures quote into dollars per bushel.

    Handles eighths notation (``"511-6"`` -> 5.1175), plain cent values
    (``"511"`` -> 5.11) and already-decimal dollar values. Returns ``None``
    for blanks and for the ``"0-0"`` placeholder the feeds use for "no quote".
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Bare numbers from these feeds are cents.
        cents = float(value)
        return None if cents == 0 else round(cents / 100.0, 6)

    text = str(value).strip()
    if not text or text in {"0-0", "0", "-"}:
        return None

    m = _TICK_RE.match(text)
    if m:
        whole = int(m.group("whole"))
        frac = int(m.group("frac"))
        if frac >= 8:
            # Some feeds pad to two digits (e.g. "511-60" meaning 6/8).
            frac = frac / 10 if frac < 80 else frac / 100
        cents = whole + frac / 8.0
        if m.group("sign") == "-":
            cents = -cents
        if cents == 0:
            return None
        return round(cents / 100.0, 6)

    m = _MONEY_RE.match(text)
    if m:
        num = float(m.group("num").replace(",", ""))
        if "-" in (m.group("pre"), m.group("post")):
            num = -num
        if num == 0:
            return None
        # A "$" prefix or a decimal point means it is already dollars.
        return round(num, 6) if ("$" in text or "." in text) else round(num / 100.0, 6)

    return None


def parse_money(value) -> float | None:
    """Parse a cash price like ``"$4.78"``, ``"4.78"`` or ``4.78`` into dollars."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    text = str(value).strip()
    if not text or text == "-":
        return None
    m = _MONEY_RE.match(text)
    if not m:
        return None
    num = float(m.group("num").replace(",", ""))
    if "-" in (m.group("pre"), m.group("post")):
        num = -num
    return round(num, 6)


def parse_date(value) -> str | None:
    """Normalise the several date shapes these feeds emit into ``YYYY-MM-DD``."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%Y%m%d", "%m/%d/%y"):
        try:
            return _dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def classify_grain(*hints) -> str | None:
    """Identify corn vs soybeans from a symbol root, commodity name or heading.

    Returns ``None`` for every other commodity, which is how non-corn/bean rows
    get filtered out of the feed.
    """
    for hint in hints:
        if not hint:
            continue
        text = str(hint).strip().upper()

        # Symbol roots are exact, so check them before the fuzzy name matching.
        if text in {"ZC", "C", "ZCE"} or re.fullmatch(r"ZC[FGHJKMNQUVXZ]\d{1,2}", text):
            return CORN
        if text in {"ZS", "S", "ZSE"} or re.fullmatch(r"ZS[FGHJKMNQUVXZ]\d{1,2}", text):
            return SOYBEANS

        # Reject the derivative products before the substring match below can
        # mistake "SOYBEAN MEAL" or "HIGH MOISTURE CORN" for the cash commodity.
        if any(bad in text for bad in ("MEAL", "OIL", "HULL", "SEED", "MOISTURE", "SILAGE")):
            continue
        if "SOYBEAN" in text or text.startswith("BEAN") or " BEANS" in text:
            return SOYBEANS
        if "CORN" in text:
            return CORN
    return None


def format_delivery_label(start: str | None, end: str | None) -> str | None:
    """Render a delivery window the way a grain merchandiser would say it.

    ``2026-10-01``..``2026-11-30`` becomes ``"Oct/Nov 2026"``; a window inside a
    single month collapses to ``"Oct 2026"``.
    """
    if not start:
        return None
    s = _dt.date.fromisoformat(start)
    if not end:
        return s.strftime("%b %Y")
    e = _dt.date.fromisoformat(end)
    if (s.year, s.month) == (e.year, e.month):
        return s.strftime("%b %Y")
    months = []
    cur = s.replace(day=1)
    last = e.replace(day=1)
    while cur <= last and len(months) < 12:
        months.append(cur.strftime("%b"))
        cur = (cur.replace(day=28) + _dt.timedelta(days=7)).replace(day=1)
    if s.year != e.year:
        return f"{s.strftime('%b %Y')}-{e.strftime('%b %Y')}"
    return f"{'/'.join(months)} {s.year}"


def futures_month_from_symbol(symbol: str | None) -> str | None:
    """Reduce ``"ZCU26"`` to the ``"CU26"`` shorthand the bid tables print."""
    if not symbol:
        return None
    text = str(symbol).strip().upper()
    m = re.fullmatch(r"Z?([CS])([FGHJKMNQUVXZ])(\d{1,2})", text)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    return text or None
