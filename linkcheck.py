"""Check whether an application link is still live and still accepting.

Two different questions, with very different reliability:

  1. Is the link dead?  HTTP 404/410 answers this well.
  2. Is the posting closed?  Only answerable when the page is server-rendered.
     Workday, iCIMS and similar ship a JavaScript shell, so there is no text to
     read and the honest answer is UNKNOWN.

Nothing here ever deletes a row. A wrong DEAD costs a glance; a wrong deletion
would silently lose a real listing.
"""

import concurrent.futures
import logging
import re

import requests

log = logging.getLogger(__name__)

OPEN = "OPEN"
CLOSED = "CLOSED"
DEAD = "DEAD"
UNKNOWN = "UNKNOWN"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = 12
MAX_WORKERS = 10

# Phrases that appear on a closed posting. Deliberately specific: generic words
# like "closed" or "expired" appear in unrelated page furniture.
CLOSED_PHRASES = [
    "no longer accepting application",
    "we are no longer accepting",
    "this job is no longer available",
    "this position is no longer available",
    "job is no longer available",
    "position has been filled",
    "this role has been filled",
    "posting has expired",
    "this job has expired",
    "job posting is closed",
    "applications are now closed",
    "applications have closed",
    "this position is closed",
    "no longer open for applications",
    "job requisition is closed",
    "sorry, this job is not available",
    "the job you are looking for is no longer",
]

NOT_FOUND_PHRASES = [
    "page not found",
    "job not found",
    "404 not found",
    "position not found",
]

# A response this small is a JavaScript shell, not a rendered posting. Reading
# it would produce a confident wrong answer, so it becomes UNKNOWN instead.
MIN_RENDERED_BYTES = 4000

_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_MARKUP = re.compile(r"<[^>]+>")


def _visible_text(html):
    """Strip scripts, styles and tags so phrase matching sees only page copy."""
    text = _TAGS.sub(" ", html)
    text = _MARKUP.sub(" ", text)
    return re.sub(r"\s+", " ", text).lower()


def classify(url, session=None):
    """Return (status, detail) for a single application URL."""
    getter = session or requests
    try:
        response = getter.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                # Without an explicit Accept, some servers answer 406.
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=TIMEOUT,
            allow_redirects=True,
        )
    except requests.Timeout:
        return UNKNOWN, "timeout"
    except requests.RequestException as exc:
        # A transient network failure is not evidence the posting is gone.
        # Only an explicit 404/410 earns DEAD.
        return UNKNOWN, type(exc).__name__

    code = response.status_code
    if code in (404, 410):
        return DEAD, f"HTTP {code}"
    if code in (401, 403, 429):
        # Bot-blocked or rate-limited. Says nothing about the posting.
        return UNKNOWN, f"HTTP {code}"
    if code >= 500:
        return UNKNOWN, f"HTTP {code}"
    if code != 200:
        return UNKNOWN, f"HTTP {code}"

    html = response.text or ""
    if len(html) < MIN_RENDERED_BYTES:
        return UNKNOWN, "js-shell"

    text = _visible_text(html)
    for phrase in CLOSED_PHRASES:
        if phrase in text:
            return CLOSED, phrase
    for phrase in NOT_FOUND_PHRASES:
        if phrase in text:
            return DEAD, phrase

    return OPEN, "HTTP 200"


def check_all(urls, max_workers=MAX_WORKERS):
    """Check many URLs concurrently. Returns {url: (status, detail)}."""
    unique = list(dict.fromkeys(urls))
    if not unique:
        return {}

    log.info("Checking %d application link(s)...", len(unique))
    results = {}

    with requests.Session() as session:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(classify, url, session): url for url in unique}
            for future in concurrent.futures.as_completed(futures):
                url = futures[future]
                try:
                    results[url] = future.result()
                except Exception as exc:  # noqa: BLE001 - a checker crash must not kill the run
                    results[url] = (UNKNOWN, type(exc).__name__)

    tally = {}
    for status, _ in results.values():
        tally[status] = tally.get(status, 0) + 1
    log.info("Link check: %s", ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return results
