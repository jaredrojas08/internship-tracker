"""One protocol layer for every applicant tracking system this project reads.

studios.py uses it to list a board's jobs; enrich.py uses it to fetch one
posting's text. Keeping both here stops two Workday clients drifting apart.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from dataclasses import dataclass
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
