"""Check whether an application link is still live and still accepting.

Two different questions, with very different reliability:

  1. Is the link dead?  HTTP 404/410 answers this well.
  2. Is the posting closed?  Only answerable when the page is server-rendered.
     Workday, iCIMS and similar ship a JavaScript shell, so there is no text to
     read and the honest answer is UNKNOWN.

Nothing here ever deletes a row. A wrong DEAD costs a glance; a wrong deletion
would silently lose a real listing.
"""

import calendar
import concurrent.futures
import datetime as dt
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

# --- Application deadline extraction ---------------------------------------

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
# Written dates ("January 15, 2027" / "15 Jan 2027") and numeric ones.
_DATE = (
    rf"(?:(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{1,2}}\s+(?:{_MONTHS})\.?,?\s+\d{{4}}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    # Month and year with no day ("deadline: december 2026"). Resolved to the
    # last day of that month — the latest the deadline could be — so a vague
    # date can never sink a listing earlier than it deserves.
    rf"|(?:{_MONTHS})\.?\s+\d{{4}})"
)
# The label must precede the date. Bare dates on a job page are almost always
# the start date, posting date, or something unrelated.
_DEADLINE_CUES = (
    r"application deadline",
    r"deadline to apply",
    # Bare "deadline" is safe only because a date must still follow inside the
    # same clause; form questions like "offer deadline, organization, role"
    # have no date after them and are therefore skipped.
    r"deadline",
    r"apply by",
    r"apply before",
    r"applications? close[sd]?(?:\s+on)?",
    r"accepting applications (?:until|through)",
    r"submit(?:\s+your)?\s+application by",
    r"last day to apply",
    r"closing date",
    r"applications? due",
    # Google and others phrase the deadline as a window that stays open until a
    # date, which reads as availability rather than a deadline.
    r"application window (?:is |will be |will remain )?open (?:until|through)",
    r"applications? (?:are |will be |will remain )?(?:open|accepted) (?:until|through)",
    r"window (?:is |will be )?open (?:until|through)",
    r"posting (?:will )?close[sd]?(?:\s+on)?",
)
# The gap allows a few filler words ("deadline *is* March 1") but no sentence
# break, so a cue can't reach across into an unrelated date.
_DEADLINE = re.compile(
    r"(?:" + "|".join(_DEADLINE_CUES) + r")[^.!?|]{0,24}?(" + _DATE + r")",
    re.IGNORECASE,
)

_NUMERIC_MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "january february march april may june july august september "
            "october november december"
        ).split(),
        start=1,
    )
}
_ABBREV = {m[:3]: i for m, i in _NUMERIC_MONTHS.items()}
_ABBREV["sept"] = 9


def _month_number(name):
    name = name.lower().rstrip(".")
    return _NUMERIC_MONTHS.get(name) or _ABBREV.get(name[:4]) or _ABBREV.get(name[:3])


def parse_deadline_date(text):
    """Normalize an extracted date string to ISO, or None if unparseable."""
    raw = text.strip().rstrip(".,").lower()

    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        try:
            return dt.date(*map(int, match.groups())).isoformat()
        except ValueError:
            return None

    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", raw)
    if match:
        month, day, year = (int(g) for g in match.groups())
        year += 2000 if year < 100 else 0
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            return None

    # Month + year only: take the last day of the month.
    match = re.fullmatch(rf"({_MONTHS})\.?\s+(\d{{4}})", raw)
    if match:
        month = _month_number(match.group(1))
        if not month:
            return None
        year = int(match.group(2))
        last_day = calendar.monthrange(year, month)[1]
        try:
            return dt.date(year, month, last_day).isoformat()
        except ValueError:
            return None

    match = re.fullmatch(
        rf"({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", raw
    )
    if not match:
        match = re.fullmatch(
            rf"(\d{{1,2}})\s+({_MONTHS})\.?,?\s+(\d{{4}})", raw
        )
        if not match:
            return None
        day, month_name, year = match.group(1), match.group(2), match.group(3)
    else:
        month_name, day, year = match.group(1), match.group(2), match.group(3)

    month = _month_number(month_name)
    if not month:
        return None
    try:
        return dt.date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def extract_deadline(text):
    """Find an application deadline in page copy. Returns ISO date or ''."""
    match = _DEADLINE.search(text)
    if not match:
        return ""
    return parse_deadline_date(match.group(1)) or ""


def _visible_text(html):
    """Strip scripts, styles and tags so phrase matching sees only page copy."""
    text = _TAGS.sub(" ", html)
    text = _MARKUP.sub(" ", text)
    return re.sub(r"\s+", " ", text).lower()


def classify(url, session=None, as_of=None):
    """Return (status, detail, deadline) for a single application URL.

    deadline is an ISO date string when the page states one, else "".
    """
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
        return UNKNOWN, "timeout", ""
    except requests.RequestException as exc:
        # A transient network failure is not evidence the posting is gone.
        # Only an explicit 404/410 earns DEAD.
        return UNKNOWN, type(exc).__name__, ""

    code = response.status_code
    if code in (404, 410):
        return DEAD, f"HTTP {code}", ""
    if code in (401, 403, 429):
        # Bot-blocked or rate-limited. Says nothing about the posting.
        return UNKNOWN, f"HTTP {code}", ""
    if code >= 500 or code != 200:
        return UNKNOWN, f"HTTP {code}", ""

    html = response.text or ""
    if len(html) < MIN_RENDERED_BYTES:
        return UNKNOWN, "js-shell", ""

    text = _visible_text(html)
    deadline = extract_deadline(text)

    for phrase in CLOSED_PHRASES:
        if phrase in text:
            return CLOSED, phrase, deadline
    for phrase in NOT_FOUND_PHRASES:
        if phrase in text:
            return DEAD, phrase, ""

    # A stated deadline that has already passed closes the posting, whatever
    # the page still says.
    if deadline:
        reference = as_of or dt.date.today()
        if dt.date.fromisoformat(deadline) < reference:
            return CLOSED, f"deadline {deadline} passed", deadline

    return OPEN, "HTTP 200", deadline


def check_all(urls, max_workers=MAX_WORKERS):
    """Check many URLs concurrently. Returns {url: (status, detail, deadline)}."""
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
                    results[url] = (UNKNOWN, type(exc).__name__, "")

    tally = {}
    for status, *_ in results.values():
        tally[status] = tally.get(status, 0) + 1
    log.info("Link check: %s", ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return results
