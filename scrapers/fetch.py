"""Shared HTTP session for the bid scrapers.

These are public cash-bid pages, but they belong to small co-ops running modest
infrastructure, so this module is deliberately conservative: one request per
*source* (the platform endpoints already return every location in a single
response), a throttle between requests, real backoff on 429/5xx, and an on-disk
cache so local development never re-hits a live site.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import random
import time
import urllib.robotparser as _robotparser
from urllib.parse import urlparse

import requests

try:
    # Optional. Some co-op sites sit behind bot protection that rejects the
    # default Python TLS handshake outright (HTTP 403) while serving the same
    # public page fine to a browser. curl_cffi replays a real browser's TLS
    # fingerprint, which is enough to be served normally - we still identify
    # ourselves in the From header and obey the same throttling as every other
    # request.
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - exercised only where it isn't installed
    curl_requests = None

IMPERSONATE_PROFILE = "chrome124"

CONTACT = os.environ.get("BIDS_CONTACT", "https://github.com/")
USER_AGENT = (
    "GrainHaulingCostMap/1.0 (+{contact}) "
    "cash-bid aggregator; contact via the URL above"
).format(contact=CONTACT)

# Some hosts reject anything that does not look like a browser. Where that is
# the case we still identify ourselves honestly in the From header.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CACHE_DIR = pathlib.Path(os.environ.get("BIDS_CACHE_DIR", ".cache"))
DEFAULT_TIMEOUT = 30
MIN_INTERVAL = 2.0  # seconds between requests to the same host

_last_request_at: dict[str, float] = {}
_robots_cache: dict[str, _robotparser.RobotFileParser | None] = {}


class FetchError(RuntimeError):
    """Raised when a URL could not be retrieved after exhausting retries."""


def _throttle(host: str) -> None:
    last = _last_request_at.get(host)
    if last is not None:
        wait = MIN_INTERVAL - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_request_at[host] = time.monotonic()


def _robots_allows(url: str, user_agent: str, impersonate: bool = False) -> bool:
    """Check robots.txt, failing open if it cannot be retrieved.

    A co-op whose robots.txt is missing or unreachable should not silently
    become un-scrapable, but an explicit Disallow is always honoured.

    robots.txt is fetched the same way the page itself will be. That matters on
    bot-protected hosts: RobotFileParser.read() uses plain urllib, which such a
    host answers with 403, and the spec says a 403 on robots.txt means "disallow
    everything". Reading it with the same client we will actually use gives the
    real policy instead of an artefact of the block.
    """
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    if root not in _robots_cache:
        parser = _robotparser.RobotFileParser()
        parser.set_url(f"{root}/robots.txt")
        try:
            headers = {"User-Agent": user_agent, "From": CONTACT}
            if impersonate and curl_requests is not None:
                resp = curl_requests.get(
                    f"{root}/robots.txt", headers=headers, timeout=20,
                    impersonate=IMPERSONATE_PROFILE,
                )
            else:
                resp = requests.get(f"{root}/robots.txt", headers=headers, timeout=20)

            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            elif resp.status_code in (401, 403):
                # A genuine 401/403 on robots.txt means treat the site as
                # off-limits, per the standard.
                parser.disallow_all = True
            else:
                parser.allow_all = True
        except Exception:
            parser = None
        _robots_cache[root] = parser

    parser = _robots_cache[root]
    if parser is None:
        return True
    try:
        return parser.can_fetch(user_agent, url)
    except Exception:
        return True


def _cache_path(url: str) -> pathlib.Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{digest}.txt"


def get(
    url: str,
    *,
    referer: str | None = None,
    browser_ua: bool = False,
    retries: int = 3,
    timeout: int = DEFAULT_TIMEOUT,
    use_cache: bool | None = None,
    cache_ttl: int = 900,
    check_robots: bool = True,
    impersonate: bool = False,
) -> str:
    """Fetch ``url`` and return its body as text.

    ``use_cache`` defaults to on whenever ``BIDS_CACHE=1`` is set, which is how
    local runs avoid hammering live sites while iterating on a parser.

    Set ``impersonate`` for hosts that reject Python's TLS handshake; it needs
    the optional curl_cffi dependency and implies a browser User-Agent.
    """
    if use_cache is None:
        use_cache = os.environ.get("BIDS_CACHE") == "1"

    cache_file = _cache_path(url)
    if use_cache and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < cache_ttl:
            return cache_file.read_text(encoding="utf-8")

    if impersonate and curl_requests is None:
        raise FetchError(
            "this source needs the curl_cffi package (pip install curl_cffi)"
        )

    agent = BROWSER_UA if (browser_ua or impersonate) else USER_AGENT
    if check_robots and not _robots_allows(url, agent, impersonate):
        raise FetchError(f"robots.txt disallows fetching {url}")

    headers = {
        "User-Agent": agent,
        "Accept": "text/html,application/json,text/javascript,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "From": CONTACT,
    }
    if referer:
        headers["Referer"] = referer

    host = urlparse(url).netloc
    last_error: Exception | None = None

    for attempt in range(retries):
        _throttle(host)
        try:
            if impersonate:
                response = curl_requests.get(
                    url, headers=headers, timeout=timeout,
                    impersonate=IMPERSONATE_PROFILE,
                )
            else:
                response = requests.get(url, headers=headers, timeout=timeout)
        except Exception as exc:  # curl_cffi raises its own error types
            last_error = exc
        else:
            if response.status_code == 200 and response.text.strip():
                if use_cache:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(response.text, encoding="utf-8")
                return response.text
            # 202 with an empty body is what the bot-protection layers return
            # while they run their JS challenge; treat it as a failure.
            last_error = FetchError(
                f"HTTP {response.status_code} ({len(response.text)} bytes) for {url}"
            )
            if response.status_code in (401, 403, 404) and attempt == 0:
                # Not worth three tries; these do not resolve by waiting.
                retries = 2

        if attempt < retries - 1:
            # Exponential backoff with jitter. Landus returns 429 on a single
            # cold request, so the first sleep is already meaningful.
            delay = (2 ** attempt) * 5 + random.uniform(0, 2)
            time.sleep(delay)

    raise FetchError(f"failed to fetch {url}: {last_error}")
