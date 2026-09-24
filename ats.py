"""One protocol layer for every applicant tracking system this project reads.

studios.py uses it to list a board's jobs; enrich.py uses it to fetch one
posting's text. Keeping both here stops two Workday clients drifting apart.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests

log = logging.getLogger(__name__)

TIMEOUT = 20
USER_AGENT = "InternshipTracker (https://github.com/jaredrojas08/Internship-Tracker, 1.0)"

SKIP_TAGS = {"script", "style", "noscript", "svg"}


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping script and style bodies."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in ("p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Visible text from an HTML document, with runs of blank lines collapsed."""
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed markup must not kill a run
        return re.sub(r"<[^>]+>", " ", html)
    text = "".join(parser.parts)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _workday_description_html(payload: dict) -> str:
    info = payload.get("jobPostingInfo") or {}
    return info.get("jobDescription", "")


def workday_description(payload: dict) -> str:
    """Plain text from a Workday CXS job payload."""
    return html_to_text(_workday_description_html(payload))


def _looks_truncated(markup: str, text: str) -> bool:
    """A big markup blob reduced to almost no text is a parse failure, not a short posting.

    An unclosed <script> or <style> tag makes HTMLParser treat the rest of the
    document as CDATA, silently dropping it. That must not present as ok=True.
    """
    return len(markup) > 2000 and len(text) < 200


@dataclass
class PageData:
    """One posting page, reduced to what enrich.py needs."""
    text: str = ""
    html: str = ""
    ok: bool = False


def _workday_api_url(url: str) -> str:
    """Turn a Workday careers URL into its CXS JSON endpoint, or '' if not Workday."""
    # The locale segment is "en-US", so it cannot be matched with [a-z-]+.
    match = re.match(r"https://([\w.-]+)\.myworkdayjobs\.com/(?:[\w-]+/)?([^/]+)(/job/.+)$", url)
    if not match:
        return ""
    host, site, path = match.groups()
    tenant = host.split(".")[0]
    return f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"


def fetch_page(url: str, session: requests.Session) -> PageData:
    """Fetch one posting. Never raises: an unreadable page is a PageData(ok=False)."""
    if not url:
        return PageData()
    try:
        api = _workday_api_url(url)
        if api:
            resp = session.get(api, timeout=TIMEOUT,
                               headers={"Accept": "application/json"})
            resp.raise_for_status()
            description_html = _workday_description_html(resp.json())
            text = html_to_text(description_html)
            if _looks_truncated(description_html, text):
                return PageData()
            return PageData(text=text, ok=True)
        resp = session.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        body = resp.text
        text = html_to_text(body)
        if _looks_truncated(body, text):
            return PageData()
        return PageData(text=text, html=body, ok=True)
    except Exception as exc:  # noqa: BLE001 - a dead posting must not fail the run
        log.debug("could not fetch %s: %s", url[:80], exc)
        return PageData()


# Most boards publish a schema.org JobPosting block with the employer's own
# timestamp. It is frequently a bare date with no time of day, which is why
# this returns a precision alongside the value instead of just the value.
DATE_KEYS = ("datePosted", "postedDate", "posted_at", "publishedAt",
             "first_published", "createdAt", "postedOn", "publishTime")
DATE_VALUE_RE = re.compile(r'"(?:' + "|".join(DATE_KEYS) + r')"\s*:\s*"([^"]{4,40})"')
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def extract_posted_at(html: str) -> tuple[datetime | None, str]:
    """Find the employer's own posting timestamp in an embedded JSON block.

    Returns (when, precision): "scraped" for a real time of day, "day" for a
    bare calendar date, "unknown" when nothing usable was found.
    """
    for match in DATE_VALUE_RE.finditer(html):
        raw = match.group(1).strip()
        date_only = bool(DATE_ONLY_RE.match(raw))
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        # A parse landing absurdly far from now is a wrong regex match, not a real date.
        now = datetime.now(timezone.utc)
        if not (now.replace(year=now.year - 3) < when < now.replace(year=now.year + 1)):
            continue
        return when, ("day" if date_only else "scraped")
    return None, "unknown"


# Headings that introduce what a candidate needs, and the ones that end that
# section. Exact wording pinned by the migration plan, not open to tuning.
REQUIREMENT_HEADINGS = ("requirements", "qualifications", "basic qualifications",
                        "minimum qualifications", "minimum requirements",
                        "basic requirements", "required qualifications",
                        "required skills", "what you'll need", "you have",
                        "who you are", "skills")
STOP_HEADINGS = ("benefits", "compensation", "perks", "about us", "equal", "eeo",
                 "accommodation", "what we offer", "why join")
BULLET_RE = re.compile(r"^\s*[-*•●▪‣⁃\d]+[.)]?\s+")


def _matches_heading(probe: str, name: str) -> bool:
    """Whole-word match: "skillset" must not match the heading "skills"."""
    if probe == name:
        return True
    if not probe.startswith(name):
        return False
    return not probe[len(name)].isalnum()


def _is_heading(line: str, names: tuple, max_len: int = 60) -> bool:
    probe = line.strip().strip(":").lower()
    if max_len and len(probe) > max_len:
        return False
    return any(_matches_heading(probe, n) for n in names)


def _contains_heading_word(line: str, names: tuple) -> bool:
    """Whether any name shows up anywhere in the line as a whole word.

    Catches a merged line like "Requirements and Benefits", where a stop word
    rides along with what would otherwise be a legitimate opening heading.
    """
    probe = line.strip().strip(":").lower()
    return any(re.search(rf"\b{re.escape(n)}\b", probe) for n in names)


def extract_requirements(text: str, max_chars: int = 900) -> str:
    """Pull the requirements/qualifications section out of a job description.

    Returns the employer's own words, not a summary, since no model is in the
    loop here. "" when the posting has no recognisable requirements section.
    """
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines()]

    start = None
    for i, line in enumerate(lines):
        if not line:
            continue
        # A heading that also carries a stop word (e.g. "Requirements and
        # Benefits") must not open a section that then bleeds into that
        # boilerplate. Skip it and keep looking rather than capture it.
        if _is_heading(line, REQUIREMENT_HEADINGS) and not _contains_heading_word(line, STOP_HEADINGS):
            start = i + 1
            break
    if start is None:
        return ""

    collected = []
    for line in lines[start:]:
        if not line:
            continue
        # No length cap here: a long sentence that opens with "Equal
        # employment opportunity..." is still boilerplate, not a bullet.
        if _is_heading(line, STOP_HEADINGS, max_len=0):
            break
        if _is_heading(line, REQUIREMENT_HEADINGS):
            continue  # a second requirements-style heading continues the same idea
        item = BULLET_RE.sub("", line).strip(" ;")
        if len(item) < 8:
            continue
        collected.append(item)
        if sum(len(c) + 2 for c in collected) > max_chars:
            break

    if not collected:
        return ""
    out = "; ".join(collected)
    if len(out) <= max_chars:
        return out
    # Reserve room for the ellipsis so the result never exceeds max_chars.
    return out[:max_chars - 3].rstrip(" ;") + "..."


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# The applicant tracking vendor's own addresses, not the employer's.
VENDOR_EMAIL_DOMAINS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
                        "workday.com", "smartrecruiters.com", "icims.com", "taleo.net",
                        "avature.net")
# Automated senders: nobody reads these inboxes.
AUTOMATED_LOCAL_RE = re.compile(r"no-?reply|do-?not-?reply|donotreply|postmaster|mailer-daemon")
# A local part naming a hiring function wins over a generic address on the same page.
HIRING_LOCAL_PARTS = ("recruit", "campus", "university", "talent", "careers", "jobs",
                     "hiring", "intern")


def extract_contact_email(html: str) -> str:
    """The single best contact address printed on the page itself, or "".

    Only an address that literally appears in the markup counts; nothing is
    ever built from a name plus a guessed company domain.
    """
    if not html:
        return ""
    seen, best_generic = set(), ""
    for match in EMAIL_RE.finditer(html):
        email = match.group(0).strip(".,;:)").lower()
        if email in seen:
            continue
        seen.add(email)
        local, _, domain = email.partition("@")
        if any(vendor in domain for vendor in VENDOR_EMAIL_DOMAINS):
            continue
        if AUTOMATED_LOCAL_RE.search(local):
            continue
        if any(hint in local for hint in HIRING_LOCAL_PARTS):
            return email  # a hiring inbox: stop looking
        if not best_generic:
            best_generic = email  # keep the first plausible one as a fallback
    return best_generic


# Notable facts worth knowing before applying. Everything here is quoted or
# labelled straight from the posting; nothing is inferred.
PAY_RANGE_RE = re.compile(
    r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?\s*(?:-|–|—|to)\s*\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?")
PAY_RATE_RE = re.compile(
    r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?\s*(?:/|\s*per\s+)(?:hour|hr|month|mo|year|yr|annum)", re.I)
DURATION_RE = re.compile(r"\b(\d{1,2})[\s-]*(?:week|month)s?\b(?!\s*(?:of|notice))", re.I)
GPA_RE = re.compile(r"(?:minimum\s+)?(?:GPA|grade point average)[^.\n]{0,24}?(\d\.\d{1,2})|"
                    r"(\d\.\d{1,2})\s*(?:GPA|or higher GPA)", re.I)

NOTE_PHRASES = (
    ("No visa sponsorship", ("not able to sponsor", "unable to sponsor",
                             "no sponsorship", "does not offer sponsorship",
                             "will not sponsor", "not provide sponsorship",
                             "not offer visa", "not eligible for visa sponsorship",
                             "not eligible for sponsorship")),
    ("US work authorization required", ("authorized to work in the united states",
                                        "must be authorized to work",
                                        "legally authorized to work")),
    ("US citizenship required", ("must be a u.s. citizen", "u.s. citizenship is required",
                                 "us citizenship required", "united states citizen")),
    ("Security clearance", ("security clearance", "able to obtain a clearance")),
    ("Relocation or housing support", ("relocation assistance", "housing stipend",
                                       "corporate housing", "relocation package",
                                       "housing is provided")),
    ("Return offer possible", ("return offer", "full-time offer upon",
                               "conversion to full-time")),
    ("Remote", ("fully remote", "100% remote", "remote-first")),
)


def extract_notes(text: str) -> str:
    """Short notable facts worth knowing before applying: pay, duration, GPA cutoff,
    sponsorship, and similar flags. Quoted or labelled from the posting, never guessed.
    """
    if not text:
        return ""
    notes = []
    low = text.lower()

    pay = PAY_RANGE_RE.search(text) or PAY_RATE_RE.search(text)
    if pay:
        notes.append("Pay: " + re.sub(r"\s+", " ", pay.group(0)).strip())

    duration = DURATION_RE.search(text)
    if duration:
        unit = "week" if "week" in duration.group(0).lower() else "month"
        notes.append(f"{duration.group(1)}-{unit} programme")

    gpa = GPA_RE.search(text)
    if gpa:
        notes.append("GPA " + (gpa.group(1) or gpa.group(2)))

    for label, phrases in NOTE_PHRASES:
        if any(phrase in low for phrase in phrases):
            notes.append(label)

    return " · ".join(notes)
