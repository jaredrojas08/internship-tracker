# Notion Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Google Sheets output with a Notion database modelled on `maiaschlesiger/internships`, keeping this repo's sources, filters and Discord digest.

**Architecture:** The pipeline is already `sources -> dedupe -> filter -> sink`. This swaps the sink and inserts two stages before it: `ats.py` fetches each new listing's posting page, `enrich.py` pulls requirements, notes and a recruiter address out of that page. `parser.Listing` grows the fields Notion needs rather than introducing a second record type.

**Tech Stack:** Python 3.12, `requests`, `unittest` (stdlib, no new test dependency), Notion API `2022-06-28`.

**Spec:** `docs/superpowers/specs/2026-09-24-notion-migration-design.md`

## Global Constraints

- **No Anthropic or Apollo key.** No code path may require either. Skill requirements are lifted verbatim from the posting; a page that cannot be read yields the literal string `See posting`.
- **Notion API version header is `2022-06-28`** on every request.
- **Notion allows roughly 3 requests/second.** Sleep `0.35` between page writes; retry `429` and `5xx` with backoff, honouring `Retry-After`.
- **Job ID is the dedup key.** It must be stable across runs for a given listing.
- **Never rewrite an existing page.** Dedup skips it. Only the explicit backfill path updates a row, and only its Skill Requirements and Recruiter Contact.
- **Property names are the single source of truth** in `notion_sink.py`. Changing a name without changing it in Notion creates a duplicate column.
- **One bad row must never lose the rest.** Per-row writes catch and log.
- **Reference implementation** is cloned at `/tmp/.../scratchpad/maia`; re-clone with `gh repo clone maiaschlesiger/internships`. Read it, do not copy blind: its `Posting` model and source list differ from ours.
- **Tests run with** `python -m unittest discover -s tests`.

---

### Task 1: Listing carries what Notion needs

`parser.Listing` has company/role/location/apply_url/source/salary. Notion needs a stable id, a posting time with a precision label, a category, keywords, skills, a recruiter address, notes and a deadline. Extend the one model rather than adding a second.

**Files:**
- Modify: `parser.py:26-55` (the `Listing` dataclass)
- Create: `tests/__init__.py` (empty)
- Test: `tests/test_listing.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Listing.job_id -> str`, `Listing.is_niche() -> bool`, and the fields `posted_at: datetime|None`, `posted_precision: str`, `category: str`, `resume_keywords: list[str]`, `skills: list[str]`, `recruiter: str`, `notes: str`, `portal_url: str`, `deadline: str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_listing.py
import unittest
from datetime import datetime, timezone

import parser as md


def make(**kw):
    base = dict(company="Riot Games", role="Software Engineer Intern",
                location="Los Angeles, CA",
                apply_url="https://boards.greenhouse.io/riotgames/jobs/1234567")
    base.update(kw)
    return md.Listing(**base)


class TestJobId(unittest.TestCase):
    def test_job_id_is_stable_across_url_cosmetics(self):
        a = make(apply_url="https://boards.greenhouse.io/riotgames/jobs/1234567")
        b = make(apply_url="https://job-boards.greenhouse.io/riotgames/jobs/1234567?gh_jid=1234567")
        self.assertEqual(a.job_id, b.job_id)

    def test_job_id_differs_for_different_postings(self):
        a = make(apply_url="https://boards.greenhouse.io/riotgames/jobs/1234567")
        b = make(apply_url="https://boards.greenhouse.io/riotgames/jobs/7654321")
        self.assertNotEqual(a.job_id, b.job_id)

    def test_job_id_is_a_plain_string(self):
        self.assertIsInstance(make().job_id, str)
        self.assertTrue(make().job_id)


class TestNiche(unittest.TestCase):
    def test_studio_board_is_niche(self):
        self.assertTrue(make(source="studios").is_niche())

    def test_github_aggregator_is_not_niche(self):
        self.assertFalse(make(source="speedyapply").is_niche())
        self.assertFalse(make(source="sndsh404").is_niche())


class TestNewFields(unittest.TestCase):
    def test_defaults_are_empty_not_none(self):
        listing = make()
        self.assertEqual(listing.resume_keywords, [])
        self.assertEqual(listing.skills, [])
        self.assertEqual(listing.recruiter, "")
        self.assertEqual(listing.notes, "")
        self.assertEqual(listing.deadline, "")
        self.assertIsNone(listing.posted_at)
        self.assertEqual(listing.posted_precision, "unknown")

    def test_mutable_defaults_are_not_shared(self):
        a, b = make(), make()
        a.resume_keywords.append("Unity")
        self.assertEqual(b.resume_keywords, [])

    def test_posted_at_accepts_aware_datetime(self):
        when = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(make(posted_at=when).posted_at, when)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m unittest discover -s tests -v`
Expected: FAIL, `TypeError: Listing.__init__() got an unexpected keyword argument 'posted_at'` and `AttributeError: 'Listing' object has no attribute 'job_id'`

- [ ] **Step 3: Write minimal implementation**

Add the imports at the top of `parser.py`:

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
```

Then extend the dataclass. `job_id` reuses `sources.url_fingerprint`, which already collapses Greenhouse host aliases and tracking params, so the two are guaranteed not to drift. Import it lazily inside the property because `sources` imports `parser`.

```python
    # --- Notion-side fields, filled in by enrich.py -------------------------
    portal_url: str = ""          # resolved employer page; falls back to apply_url
    posted_at: Optional[datetime] = None   # always tz-aware UTC
    # How much to trust posted_at: "scraped" | "commit" | "first_seen" | "day"
    posted_precision: str = "unknown"
    category: str = ""
    resume_keywords: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    recruiter: str = ""
    notes: str = ""
    deadline: str = ""            # ISO date, user-owned once written

    # Lists that syndicate the same few hundred well-known postings. A listing
    # none of them carried came from a smaller board, which is the interesting
    # case.
    MAINSTREAM_SOURCES = ("sndsh404", "speedyapply")

    @property
    def job_id(self):
        """Stable identity for Notion dedup, derived from the posting URL."""
        import sources
        host, ident = sources.url_fingerprint(self.apply_url) or ("", self.role.lower())
        return f"{host}:{ident}"

    def is_niche(self):
        """True when no mainstream aggregator carried this listing."""
        found_in = self.source.lower()
        return not any(name in found_in for name in self.MAINSTREAM_SOURCES)

    def keywords_cell(self):
        return ", ".join(self.resume_keywords)

    def skills_cell(self):
        return ", ".join(self.skills)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m unittest discover -s tests -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Verify nothing downstream broke**

Run: `set -a && source .env && set +a && ./venv/bin/python internship_tracker.py --dry-run --skip-links --no-style --no-notify 2>&1 | grep -E "on the sheet|ERROR|Traceback"`
Expected: `682 listing(s) on the sheet after this run`, no traceback

- [ ] **Step 6: Commit**

```bash
git add parser.py tests/
git commit -m "feat: Listing carries the fields the Notion sink needs"
```

---

### Task 2: One ATS protocol layer

`studios.py` speaks Greenhouse, Ashby, Lever, Workday and Avature to *list* jobs. Maia's `jobdesc.py` speaks the same protocols to *fetch one job's page*. Two Workday clients would drift. `ats.py` owns the protocol layer.

**Files:**
- Create: `ats.py`
- Test: `tests/test_ats.py`
- Create: `tests/fixtures/greenhouse_job.html`, `tests/fixtures/workday_job.json`

**Interfaces:**
- Consumes: nothing
- Produces: `ats.html_to_text(html: str) -> str`, `ats.fetch_page(url: str, session: requests.Session) -> PageData`, and `PageData` with fields `text: str`, `html: str`, `ok: bool`

- [ ] **Step 1: Capture real fixtures**

Do not hand-write these. Save two real payloads:

```bash
mkdir -p tests/fixtures
curl -sL -A "Mozilla/5.0" "https://job-boards.greenhouse.io/riotgames/jobs/7016915" \
  -o tests/fixtures/greenhouse_job.html
# Workday's job-DETAIL endpoint is GET. Only the job-LIST endpoint takes POST.
curl -s "https://bah.wd1.myworkdayjobs.com/wday/cxs/bah/bah_jobs/job/McLean-VA/University--2027-Summer-Games-Software-Developer-Intern---McLean--VA_R0249827" \
  -H "Accept: application/json" \
  -o tests/fixtures/workday_job.json
```

If either URL is dead, substitute any live posting from the same ATS and note the substitution in the test docstring.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_ats.py
import json
import pathlib
import unittest

import ats

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class TestHtmlToText(unittest.TestCase):
    def test_strips_tags_and_keeps_words(self):
        text = ats.html_to_text("<div><p>Hello <b>world</b></p></div>")
        self.assertIn("Hello", text)
        self.assertIn("world", text)
        self.assertNotIn("<", text)

    def test_drops_script_and_style_content(self):
        text = ats.html_to_text("<style>.a{color:red}</style><script>var x=1</script><p>Keep</p>")
        self.assertIn("Keep", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("var x", text)

    def test_unescapes_entities(self):
        self.assertIn("R&D", ats.html_to_text("<p>R&amp;D</p>"))

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(ats.html_to_text(""), "")


class TestRealPayloads(unittest.TestCase):
    def test_greenhouse_page_yields_substantial_text(self):
        html = (FIXTURES / "greenhouse_job.html").read_text(encoding="utf-8")
        text = ats.html_to_text(html)
        self.assertGreater(len(text), 500)

    def test_workday_json_yields_description_text(self):
        payload = json.loads((FIXTURES / "workday_job.json").read_text(encoding="utf-8"))
        text = ats.workday_description(payload)
        self.assertGreater(len(text), 500)
        self.assertNotIn("<li>", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./venv/bin/python -m unittest tests.test_ats -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'ats'`

- [ ] **Step 4: Write the implementation**

Read `/tmp/.../maia/radar/jobdesc.py` lines 51-124 for `_TextExtractor`, `html_to_text` and `_from_embedded_json`. Port them into `ats.py` unchanged, then add the Workday branch this repo needs.

```python
# ats.py
"""One protocol layer for every applicant tracking system this project reads.

studios.py uses it to list a board's jobs; enrich.py uses it to fetch one
posting's text. Keeping both here stops two Workday clients drifting apart.
"""

from __future__ import annotations

import html as html_module
import json
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


def workday_description(payload: dict) -> str:
    """Plain text from a Workday CXS job payload."""
    info = payload.get("jobPostingInfo") or {}
    return html_to_text(info.get("jobDescription", ""))


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
            return PageData(text=workday_description(resp.json()), ok=True)
        resp = session.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        body = resp.text
        return PageData(text=html_to_text(body), html=body, ok=True)
    except Exception as exc:  # noqa: BLE001 - a dead posting must not fail the run
        log.debug("could not fetch %s: %s", url[:80], exc)
        return PageData()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./venv/bin/python -m unittest tests.test_ats -v`
Expected: PASS, 6 tests

- [ ] **Step 6: Commit**

```bash
git add ats.py tests/test_ats.py tests/fixtures/
git commit -m "feat: add ats.py, one protocol layer for posting pages"
```

---

### Task 3: Pull facts out of a posting

Pure string functions, no network. These are the highest-value tests in the plan because they encode judgement calls that are easy to get subtly wrong.

**Files:**
- Modify: `ats.py` (append)
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `ats.html_to_text` from Task 2
- Produces: `ats.extract_requirements(text: str, max_chars: int = 900) -> str`, `ats.extract_contact_email(html: str) -> str`, `ats.extract_notes(text: str) -> str`, `ats.extract_posted_at(html: str) -> tuple[datetime|None, str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extract.py
import unittest
from datetime import timezone

import ats

POSTING = """
About the role
We are looking for an intern to join the gameplay team.

Basic Qualifications
- Currently pursuing a BS in Computer Science
- Experience with C# and Unity
- Graduating between December 2027 and June 2028

Benefits
We offer medical, dental and a 401(k) match.

Equal Employment Opportunity
We are an equal opportunity employer. Accommodations will be provided.
"""


class TestRequirements(unittest.TestCase):
    def test_lifts_the_qualifications_section(self):
        out = ats.extract_requirements(POSTING)
        self.assertIn("BS in Computer Science", out)
        self.assertIn("C# and Unity", out)

    def test_stops_before_benefits(self):
        out = ats.extract_requirements(POSTING)
        self.assertNotIn("401(k)", out)

    def test_drops_eeo_boilerplate(self):
        out = ats.extract_requirements(POSTING)
        self.assertNotIn("equal opportunity", out.lower())

    def test_no_recognised_heading_returns_empty(self):
        self.assertEqual(ats.extract_requirements("Just some prose about us."), "")

    def test_truncates_to_max_chars(self):
        long_text = "Requirements\n" + ("- a very long bullet line\n" * 200)
        self.assertLessEqual(len(ats.extract_requirements(long_text, max_chars=300)), 300)


class TestContactEmail(unittest.TestCase):
    def test_prefers_a_hiring_local_part(self):
        html = '<p>legal@acme.com</p><p>campus.recruiting@acme.com</p>'
        self.assertEqual(ats.extract_contact_email(html), "campus.recruiting@acme.com")

    def test_discards_ats_vendor_addresses(self):
        html = '<p>no-reply@greenhouse.io</p><p>support@lever.co</p>'
        self.assertEqual(ats.extract_contact_email(html), "")

    def test_discards_automated_senders(self):
        self.assertEqual(ats.extract_contact_email("<p>do-not-reply@acme.com</p>"), "")

    def test_returns_empty_when_no_address(self):
        self.assertEqual(ats.extract_contact_email("<p>Apply on our site.</p>"), "")

    def test_never_constructs_an_address(self):
        # A page naming a person but no address must not yield firstname@company
        self.assertEqual(ats.extract_contact_email("<p>Contact Jane Doe at Acme.</p>"), "")


class TestNotes(unittest.TestCase):
    def test_captures_pay_when_stated(self):
        out = ats.extract_notes("The projected compensation range is $53,000.00 to $108,000.00.")
        self.assertIn("$53,000", out)

    def test_captures_sponsorship_statement(self):
        out = ats.extract_notes("This role is not eligible for visa sponsorship.")
        self.assertIn("sponsorship", out.lower())

    def test_returns_empty_when_nothing_notable(self):
        self.assertEqual(ats.extract_notes("We build games."), "")


class TestPostedAt(unittest.TestCase):
    def test_reads_schema_org_dateposted_with_time(self):
        html = '<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-09-20T14:30:00Z"}</script>'
        when, precision = ats.extract_posted_at(html)
        self.assertIsNotNone(when)
        self.assertEqual(when.tzinfo, timezone.utc)
        self.assertEqual(precision, "scraped")

    def test_bare_date_is_marked_day_precision(self):
        html = '<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-09-20"}</script>'
        when, precision = ats.extract_posted_at(html)
        self.assertIsNotNone(when)
        self.assertEqual(precision, "day")

    def test_no_block_returns_none(self):
        when, precision = ats.extract_posted_at("<p>nothing here</p>")
        self.assertIsNone(when)
        self.assertEqual(precision, "unknown")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m unittest tests.test_extract -v`
Expected: FAIL, `AttributeError: module 'ats' has no attribute 'extract_requirements'`

- [ ] **Step 3: Write the implementation**

Read `/tmp/.../maia/radar/jobdesc.py` lines 125-364 for `extract_posted_at`, `extract_requirements`, `extract_contact_emails` and `extract_notes`. Port them, adapting the email function to return a single best address rather than a list, and keeping these rules exactly:

- Requirement headings: `requirements`, `qualifications`, `basic qualifications`, `minimum qualifications`, `what you'll need`, `you have`, `who you are`, `skills`.
- Stop headings: `benefits`, `compensation`, `perks`, `about us`, `equal`, `eeo`, `accommodation`, `what we offer`, `why join`.
- Vendor email domains to discard: `greenhouse.io`, `lever.co`, `ashbyhq.com`, `myworkdayjobs.com`, `workday.com`, `smartrecruiters.com`, `icims.com`, `taleo.net`, `avature.net`.
- Automated local parts to discard: anything matching `no-?reply`, `do-?not-?reply`, `donotreply`, `postmaster`, `mailer-daemon`.
- Hiring local parts that win a tie: `recruit`, `campus`, `university`, `talent`, `careers`, `jobs`, `hiring`, `intern`.
- An address is only ever taken from the page. Never build one from a name and a domain.

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m unittest tests.test_extract -v`
Expected: PASS, 16 tests

- [ ] **Step 5: Commit**

```bash
git add ats.py tests/test_extract.py
git commit -m "feat: extract requirements, contact, notes and posting time from a page"
```

---

### Task 4: enrich.py wires fetching onto listings

**Files:**
- Create: `enrich.py`
- Test: `tests/test_enrich.py`

**Interfaces:**
- Consumes: `ats.fetch_page`, `ats.extract_*` from Tasks 2-3; `parser.Listing` from Task 1
- Produces: `enrich.enrich_all(listings: list[Listing], max_workers: int = 8) -> None` (mutates in place), `enrich.KEYWORDS_BY_CATEGORY: dict[str, list[str]]`, `enrich.GENERIC_SKILLS = "See posting"`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrich.py
import unittest
from unittest import mock

import ats
import enrich
import parser as md


def make(**kw):
    base = dict(company="Riot Games", role="Gameplay Programmer Intern",
                location="Los Angeles, CA", apply_url="https://example.com/jobs/1234567",
                source="studios", from_game_studio=True)
    base.update(kw)
    return md.Listing(**base)


class TestEnrich(unittest.TestCase):
    def test_unreadable_page_falls_back_to_see_posting(self):
        listing = make()
        with mock.patch.object(ats, "fetch_page", return_value=ats.PageData(ok=False)):
            enrich.enrich_all([listing])
        self.assertEqual(listing.skills, [enrich.GENERIC_SKILLS])

    def test_unreadable_page_still_gets_category_keywords(self):
        listing = make()
        with mock.patch.object(ats, "fetch_page", return_value=ats.PageData(ok=False)):
            enrich.enrich_all([listing])
        self.assertTrue(listing.resume_keywords)

    def test_readable_page_uses_the_employers_words(self):
        page = ats.PageData(text="Qualifications\n- Experience with Unity and C#\n", ok=True)
        listing = make()
        with mock.patch.object(ats, "fetch_page", return_value=page):
            enrich.enrich_all([listing])
        self.assertIn("Unity", listing.skills_cell())

    def test_enrichment_never_raises_on_a_bad_listing(self):
        listing = make(apply_url="")
        enrich.enrich_all([listing])  # must not raise
        self.assertEqual(listing.skills, [enrich.GENERIC_SKILLS])

    def test_category_is_set_from_the_role(self):
        listing = make()
        with mock.patch.object(ats, "fetch_page", return_value=ats.PageData(ok=False)):
            enrich.enrich_all([listing])
        self.assertEqual(listing.category, "Game Programming")

    def test_non_game_role_gets_software_engineering_category(self):
        listing = make(role="Backend Software Engineer Intern", source="speedyapply",
                       from_game_studio=False)
        with mock.patch.object(ats, "fetch_page", return_value=ats.PageData(ok=False)):
            enrich.enrich_all([listing])
        self.assertEqual(listing.category, "Software Engineering")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m unittest tests.test_enrich -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'enrich'`

- [ ] **Step 3: Write the implementation**

```python
# enrich.py
"""Fill in the fields the Notion row wants, from the posting's own page.

Nothing here calls a model. A page that reads gives the employer's own
requirement text; a page that does not gives "See posting" and keywords
inferred from the role, and the cell says so.
"""

from __future__ import annotations

import concurrent.futures
import logging

import requests

import ats
import config

log = logging.getLogger(__name__)

GENERIC_SKILLS = "See posting"

KEYWORDS_BY_CATEGORY = {
    "Game Programming": ["Unity", "C#", "Gameplay", "Game Engine", "Unreal",
                         "State Machines", "Physics", "Animation", "Tools"],
    "Software Engineering": ["Python", "Java", "C++", "Data Structures",
                             "Algorithms", "APIs", "Git", "Testing"],
}


def category_for(listing):
    """Which Notion Category select this listing belongs in."""
    return "Game Programming" if listing.is_game else "Software Engineering"


def _enrich_one(listing, session):
    listing.category = category_for(listing)
    listing.resume_keywords = list(KEYWORDS_BY_CATEGORY.get(listing.category, []))

    page = ats.fetch_page(listing.apply_url, session)
    if not page.ok:
        listing.skills = [GENERIC_SKILLS]
        return

    requirements = ats.extract_requirements(page.text)
    listing.skills = [requirements] if requirements else [GENERIC_SKILLS]
    listing.notes = ats.extract_notes(page.text)
    # The Workday branch returns text with no html, so fall back to it.
    markup = page.html or page.text
    listing.recruiter = ats.extract_contact_email(markup)
    when, precision = ats.extract_posted_at(markup)
    if when:
        listing.posted_at, listing.posted_precision = when, precision


def enrich_all(listings, max_workers=8):
    """Fetch and enrich every listing in place. Individual failures are absorbed."""
    if not listings:
        return
    session = requests.Session()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_enrich_one, l, session) for l in listings]
        for listing, future in zip(listings, futures):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the rest
                log.warning("enrich failed for %r: %s", listing.role[:50], exc)
                if not listing.skills:
                    listing.skills = [GENERIC_SKILLS]
    readable = sum(1 for l in listings if l.skills != [GENERIC_SKILLS])
    log.info("enriched %d/%d listing(s) from their posting page", readable, len(listings))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m unittest tests.test_enrich -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add enrich.py tests/test_enrich.py
git commit -m "feat: enrich listings from their own posting page"
```

---

### Task 5: The Notion sink

**Files:**
- Create: `notion_sink.py`
- Test: `tests/test_notion_sink.py`

**Interfaces:**
- Consumes: `parser.Listing` from Task 1
- Produces: `notion_sink.Notion(token)` with `create_database(parent_page_id, title) -> str`, `ensure_schema(database_id) -> list[str]`, `existing_job_ids(database_id) -> set[str]`, `add(database_id, listing) -> None`, `add_all(database_id, listings) -> int`; module constants `SCHEMA`, `APPLIED_OPTIONS`, and `P_*` property names

- [ ] **Step 1: Write the failing test**

The test never touches the network. It substitutes a fake `_call` and asserts on the payloads.

```python
# tests/test_notion_sink.py
import unittest

import notion_sink
import parser as md


class FakeNotion(notion_sink.Notion):
    """Records every call instead of making it."""

    def __init__(self):
        super().__init__("ntn_fake")
        self.calls = []
        self.responses = []

    def _call(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        return self.responses.pop(0) if self.responses else {"id": "fake-id", "results": []}


def make(**kw):
    base = dict(company="Riot Games", role="Gameplay Programmer Intern",
                location="Los Angeles, CA",
                apply_url="https://boards.greenhouse.io/riotgames/jobs/1234567",
                source="studios", from_game_studio=True)
    base.update(kw)
    return md.Listing(**base)


class TestSchema(unittest.TestCase):
    def test_schema_has_every_documented_property(self):
        expected = {"Title", "Company", "Category", "Term", "Location",
                    "Application Portal", "Resume Keywords", "Skill Requirements",
                    "Posted", "Hours Since Posted", "Recruiter Contact", "Applied",
                    "My Resume PDF", "Source", "Job ID", "Notes", "Niche", "Deadline"}
        self.assertEqual(set(notion_sink.SCHEMA), expected)

    def test_applied_options_match_the_agreed_labels_and_colours(self):
        self.assertEqual(
            [(o["name"], o["color"]) for o in notion_sink.APPLIED_OPTIONS],
            [("Not applied", "default"), ("Applying", "yellow"), ("Applied", "blue"),
             ("Interviewing", "purple"), ("Offer", "green"), ("Rejected", "red")],
        )

    def test_deadline_is_a_date_not_text(self):
        self.assertIn("date", notion_sink.SCHEMA["Deadline"])

    def test_hours_since_posted_is_a_formula_over_posted(self):
        formula = notion_sink.SCHEMA["Hours Since Posted"]["formula"]["expression"]
        self.assertIn('prop("Posted")', formula)
        self.assertIn("hours", formula)


class TestAdd(unittest.TestCase):
    def test_writes_one_page_with_the_job_id(self):
        api = FakeNotion()
        listing = make()
        api.add("db1", listing)
        method, path, body = api.calls[-1]
        self.assertEqual((method, path), ("POST", "/pages"))
        job_id = body["properties"]["Job ID"]["rich_text"][0]["text"]["content"]
        self.assertEqual(job_id, listing.job_id)

    def test_new_row_defaults_to_not_applied(self):
        api = FakeNotion()
        api.add("db1", make())
        body = api.calls[-1][2]
        self.assertEqual(body["properties"]["Applied"]["select"]["name"], "Not applied")

    def test_studio_listing_is_marked_niche(self):
        api = FakeNotion()
        api.add("db1", make(source="studios"))
        self.assertTrue(api.calls[-1][2]["properties"]["Niche"]["checkbox"])

    def test_aggregator_listing_is_not_niche(self):
        api = FakeNotion()
        api.add("db1", make(source="speedyapply", from_game_studio=False))
        self.assertFalse(api.calls[-1][2]["properties"]["Niche"]["checkbox"])

    def test_title_links_to_the_posting(self):
        api = FakeNotion()
        listing = make()
        api.add("db1", listing)
        title = api.calls[-1][2]["properties"]["Title"]["title"][0]["text"]
        self.assertEqual(title["link"]["url"], listing.apply_url)

    def test_select_values_never_contain_a_comma(self):
        # Notion rejects a select option with a comma and fails the whole write.
        api = FakeNotion()
        api.add("db1", make(source="studios,speedyapply"))
        self.assertNotIn(",", api.calls[-1][2]["properties"]["Source"]["select"]["name"])

    def test_empty_optional_fields_are_omitted_not_sent_empty(self):
        api = FakeNotion()
        api.add("db1", make())
        props = api.calls[-1][2]["properties"]
        self.assertNotIn("Recruiter Contact", props)

    def test_add_all_survives_one_failing_row(self):
        class Flaky(FakeNotion):
            def _call(self, method, path, **kw):
                if "bad" in str(kw.get("json", "")):
                    raise RuntimeError("boom")
                return super()._call(method, path, **kw)

        api = Flaky()
        written = api.add_all("db1", [make(company="bad"), make(company="Good Co")])
        self.assertEqual(written, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m unittest tests.test_notion_sink -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'notion_sink'`

- [ ] **Step 3: Write the implementation**

Port `/tmp/.../maia/radar/notion_sink.py`. Keep her `_call`, `_select`, `_plain_text`, `create_database`, `ensure_schema`, `existing_job_ids`, `add` and `add_all` as written. Three changes for this repo:

1. Add `P_DEADLINE = "Deadline"` with `{"date": {}}` in `SCHEMA`, and write it in `add` when `listing.deadline` is a non-empty ISO date.
2. Replace her `Posting` attribute names with ours: `p.title` becomes `listing.role`, `p.listing_url` becomes `listing.apply_url`.
3. Drop `rows_awaiting_resume`, `attach_file` and `expire_stale`. No auto-tailoring, and expiry belonged to her link-freshness model which this repo dropped.

Remove `add_all`'s `time.sleep(0.35)` only if a test patches it; otherwise keep it, since it is what holds the run under Notion's rate limit.

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m unittest tests.test_notion_sink -v`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add notion_sink.py tests/test_notion_sink.py
git commit -m "feat: add the Notion sink"
```

---

### Task 6: Tombstones, so a deleted row stays deleted

Dedup runs against pages currently in the database, so deleting a row in Notion makes it reappear on the next run. The Sheet solved this with a Removed tab; Notion needs a file.

**Files:**
- Create: `tombstones.py`
- Create: `removed.json` (containing `[]`)
- Test: `tests/test_tombstones.py`

**Interfaces:**
- Consumes: nothing
- Produces: `tombstones.load(path="removed.json") -> set[str]`, `tombstones.add(job_ids: Iterable[str], path="removed.json") -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tombstones.py
import json
import tempfile
import unittest
from pathlib import Path

import tombstones


class TestTombstones(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "removed.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_loads_as_empty(self):
        self.assertEqual(tombstones.load(self.path), set())

    def test_add_then_load_round_trips(self):
        tombstones.add(["greenhouse.io:123"], self.path)
        self.assertEqual(tombstones.load(self.path), {"greenhouse.io:123"})

    def test_add_is_idempotent(self):
        tombstones.add(["a"], self.path)
        added = tombstones.add(["a"], self.path)
        self.assertEqual(added, 0)
        self.assertEqual(tombstones.load(self.path), {"a"})

    def test_file_stays_sorted_so_diffs_are_readable(self):
        tombstones.add(["c", "a", "b"], self.path)
        self.assertEqual(json.loads(self.path.read_text()), ["a", "b", "c"])

    def test_corrupt_file_loads_as_empty_rather_than_raising(self):
        self.path.write_text("{not json")
        self.assertEqual(tombstones.load(self.path), set())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m unittest tests.test_tombstones -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'tombstones'`

- [ ] **Step 3: Write the implementation**

```python
# tombstones.py
"""Job IDs the user deleted from Notion, so the next run does not re-add them.

The Sheet had a hidden Removed tab for this. Notion has no equivalent, since a
deleted page stops coming back from the API, which is exactly what makes it
look new again.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("removed.json")


def load(path=DEFAULT_PATH):
    """Every tombstoned job id. A missing or unreadable file means none."""
    try:
        return set(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def add(job_ids, path=DEFAULT_PATH):
    """Tombstone these ids. Returns how many were new."""
    current = load(path)
    incoming = {j for j in job_ids if j}
    new = incoming - current
    if new:
        Path(path).write_text(json.dumps(sorted(current | incoming), indent=1),
                              encoding="utf-8")
        log.info("tombstoned %d job id(s)", len(new))
    return len(new)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m unittest tests.test_tombstones -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add tombstones.py removed.json tests/test_tombstones.py
git commit -m "feat: tombstone job ids deleted from Notion"
```

---

### Task 7: Wire the pipeline behind --notion

The Sheets path keeps working. Both can run, which is what makes step 2 of the rollout possible.

**Files:**
- Modify: `internship_tracker.py` (argument parsing near line 23, and `main` after the dedup call near line 113)
- Modify: `requirements.txt`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: everything from Tasks 1-6
- Produces: `internship_tracker.run_notion(listings, args) -> int` returning the number of pages written

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline.py
import unittest
from unittest import mock

import internship_tracker
import parser as md


def make(job_url, **kw):
    base = dict(company="Riot Games", role="Gameplay Programmer Intern",
                location="LA", apply_url=job_url, source="studios",
                from_game_studio=True)
    base.update(kw)
    return md.Listing(**base)


class TestNotionRun(unittest.TestCase):
    def test_skips_listings_already_in_the_database(self):
        listings = [make("https://x.com/jobs/1111111"), make("https://x.com/jobs/2222222")]
        api = mock.Mock()
        api.existing_job_ids.return_value = {listings[0].job_id}
        api.add_all.return_value = 1
        internship_tracker.write_to_notion(api, "db1", listings, tombstoned=set())
        written = api.add_all.call_args[0][1]
        self.assertEqual([l.job_id for l in written], [listings[1].job_id])

    def test_skips_tombstoned_listings(self):
        listings = [make("https://x.com/jobs/1111111")]
        api = mock.Mock()
        api.existing_job_ids.return_value = set()
        internship_tracker.write_to_notion(api, "db1", listings,
                                           tombstoned={listings[0].job_id})
        self.assertEqual(api.add_all.call_args[0][1], [])

    def test_nothing_new_makes_no_write_call(self):
        listings = [make("https://x.com/jobs/1111111")]
        api = mock.Mock()
        api.existing_job_ids.return_value = {listings[0].job_id}
        internship_tracker.write_to_notion(api, "db1", listings, tombstoned=set())
        api.add_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m unittest tests.test_pipeline -v`
Expected: FAIL, `AttributeError: module 'internship_tracker' has no attribute 'write_to_notion'`

- [ ] **Step 3: Write the implementation**

Add to `internship_tracker.py`:

```python
def write_to_notion(api, database_id, listings, tombstoned):
    """Append listings Notion does not already hold. Never rewrites a page."""
    seen = api.existing_job_ids(database_id)
    fresh = [l for l in listings
             if l.job_id not in seen and l.job_id not in tombstoned]
    log.info("%d listing(s) already in Notion, %d tombstoned, %d new",
             len(listings) - len(fresh), len(tombstoned), len(fresh))
    if not fresh:
        return 0
    return api.add_all(database_id, fresh)
```

Add these arguments to `parse_args`:

```python
    ap.add_argument("--notion", action="store_true",
                    help="write to the Notion database instead of the sheet")
    ap.add_argument("--create-database", action="store_true",
                    help="create the Notion database under NOTION_PARENT_PAGE_ID and print its id")
```

In `main`, immediately after `listings, duplicates = sources.deduplicate(listings)`:

```python
    if args.create_database:
        api = notion_sink.Notion(config.require_env("NOTION_TOKEN"))
        print(api.create_database(config.require_env("NOTION_PARENT_PAGE_ID"),
                                  "Internship Listings"))
        return 0

    if args.notion:
        enrich.enrich_all(listings)
        api = notion_sink.Notion(config.require_env("NOTION_TOKEN"))
        database_id = config.require_env("NOTION_DATABASE_ID")
        api.ensure_schema(database_id)
        write_to_notion(api, database_id, listings, tombstones.load())
        # The digest call lands in Task 9, which is where send_digest_if_due
        # is written. Adding it here would raise AttributeError.
        return 0
```

Add `config.require_env`:

```python
def require_env(name):
    """Read an environment variable or fail with a message that says what to do."""
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set. Locally: add it to .env. "
            f"In CI: Settings -> Secrets and variables -> Actions."
        )
    return value
```

`requirements.txt` needs no new entry: `requests` is already there and the Notion client is plain HTTP.

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m unittest discover -s tests -v`
Expected: PASS, all tests

- [ ] **Step 5: Verify the Sheets path is untouched**

Run: `set -a && source .env && set +a && ./venv/bin/python internship_tracker.py --dry-run --skip-links --no-style --no-notify 2>&1 | grep -E "on the sheet|ERROR"`
Expected: `682 listing(s) on the sheet after this run`

- [ ] **Step 6: Commit**

```bash
git add internship_tracker.py config.py tests/test_pipeline.py
git commit -m "feat: write to Notion behind --notion"
```

---

### Task 8: Migrate the sheet

**Files:**
- Create: `migrate.py`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Consumes: `parser.Listing`, `notion_sink.Notion`
- Produces: `migrate.rows_to_listings(rows: list[dict]) -> list[Listing]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate.py
import unittest

import migrate


ROW = {
    "Company": "Riot Games", "Role": "Gameplay Programmer Intern",
    "Location": "Los Angeles, CA",
    "Apply Link": "https://boards.greenhouse.io/riotgames/jobs/1234567",
    "Salary": "", "Deadline": "2026-12-31", "Date Added": "2026-09-19",
    "Remote?": "NO", "Game?": "YES", "Source": "studios",
    "Application": "Applying", "Applied Date": "", "Remove?": "FALSE",
}


class TestMigration(unittest.TestCase):
    def test_carries_the_hand_typed_deadline(self):
        self.assertEqual(migrate.rows_to_listings([ROW])[0].deadline, "2026-12-31")

    def test_date_added_becomes_posted_at_with_first_seen_precision(self):
        listing = migrate.rows_to_listings([ROW])[0]
        self.assertEqual(listing.posted_at.date().isoformat(), "2026-09-19")
        self.assertEqual(listing.posted_precision, "first_seen")

    def test_posted_at_is_timezone_aware(self):
        self.assertIsNotNone(migrate.rows_to_listings([ROW])[0].posted_at.tzinfo)

    def test_game_column_restores_the_studio_flag(self):
        self.assertTrue(migrate.rows_to_listings([ROW])[0].is_game)

    def test_rows_marked_remove_are_dropped(self):
        row = dict(ROW, **{"Remove?": "TRUE"})
        self.assertEqual(migrate.rows_to_listings([row]), [])

    def test_blank_rows_are_dropped(self):
        self.assertEqual(migrate.rows_to_listings([{k: "" for k in ROW}]), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m unittest tests.test_migrate -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'migrate'`

- [ ] **Step 3: Write the implementation**

`migrate.py` reads the sheet with `worksheet.get_all_values()` and zips the header row onto each row, so `rows_to_listings` receives dicts keyed by the sheet's own column names exactly as the test above spells them. Do not route this through `sheets.read_listing_state`, which reshapes keys to lowercase and would not match. It then writes through `notion_sink`. The `Application` value maps straight across since the Notion select uses the same three labels plus three more. Preserve `Applied Date` into `Notes` as `Applied YYYY-MM-DD` when present, because Notion has no separate applied-date column in Maia's schema.

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m unittest tests.test_migrate -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Dry run the migration against a scratch database**

Create a throwaway database first so a bug cannot damage the real one:

```bash
set -a && source .env && set +a
NOTION_DATABASE_ID=$(./venv/bin/python internship_tracker.py --create-database)
echo "scratch db: $NOTION_DATABASE_ID"
NOTION_DATABASE_ID=$NOTION_DATABASE_ID ./venv/bin/python migrate.py --dry-run
```

Expected: prints 682 listings and 10 programs, writes nothing.

- [ ] **Step 6: Commit**

```bash
git add migrate.py tests/test_migrate.py
git commit -m "feat: migrate the sheet into Notion"
```

---

### Task 9: One digest a day, not twenty-four

**Files:**
- Modify: `notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `notify.send_digest_if_due(listings, state_path="digest_state.json", as_of=None) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notify.py
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import notify


class TestDigestCadence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "digest_state.json"
        self.now = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.dir.cleanup()

    def test_first_ever_run_is_due(self):
        self.assertTrue(notify.digest_is_due(self.path, self.now))

    def test_not_due_an_hour_after_sending(self):
        self.path.write_text(json.dumps({"last_sent": (self.now - timedelta(hours=1)).isoformat()}))
        self.assertFalse(notify.digest_is_due(self.path, self.now))

    def test_due_again_after_24_hours(self):
        self.path.write_text(json.dumps({"last_sent": (self.now - timedelta(hours=25)).isoformat()}))
        self.assertTrue(notify.digest_is_due(self.path, self.now))

    def test_corrupt_state_is_treated_as_due(self):
        self.path.write_text("{not json")
        self.assertTrue(notify.digest_is_due(self.path, self.now))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m unittest tests.test_notify -v`
Expected: FAIL, `AttributeError: module 'notify' has no attribute 'digest_is_due'`

- [ ] **Step 3: Write the implementation**

Add `digest_is_due(path, as_of)` reading a `last_sent` ISO timestamp, and `send_digest_if_due` that checks it, calls the existing `build_digest`/`send`, and records the send. Source-failure warnings bypass the check and send immediately, since a broken source is the one thing worth interrupting for.

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m unittest tests.test_notify -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add notify.py tests/test_notify.py
git commit -m "feat: batch the digest to once a day now that runs are hourly"
```

---

### Task 10: Hourly workflow

**Files:**
- Modify: `.github/workflows/update_internships.yml`

- [ ] **Step 1: Change the schedule and the command**

```yaml
on:
  schedule:
    # Hourly at :20. GitHub's scheduler is best-effort and can lag under load.
    - cron: '20 * * * *'
  workflow_dispatch:
    inputs:
      test_notify:
        description: 'Send a test digest and exit'
        type: boolean
        default: false
```

Add the Notion secrets to the run step's `env` and pass `--notion`:

```yaml
      - name: Run internship tracker
        env:
          NOTION_TOKEN: ${{ secrets.NOTION_TOKEN }}
          NOTION_DATABASE_ID: ${{ secrets.NOTION_DATABASE_ID }}
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: python internship_tracker.py --notion
```

The job now commits `removed.json` and `digest_state.json`, so `permissions` must become `contents: write`, and a commit step is needed after the run. Model it on `.github/workflows/keepalive.yml`, which already pushes from Actions.

- [ ] **Step 2: Run the tests**

Run: `./venv/bin/python -m unittest discover -s tests -v`
Expected: PASS

- [ ] **Step 3: Commit and trigger one manual run**

```bash
git add .github/workflows/update_internships.yml
git commit -m "ci: run hourly and write to Notion"
git push
gh workflow run "Update Internship Listings"
```

- [ ] **Step 4: Verify the run**

Run: `gh run watch $(gh run list --limit 1 --json databaseId --jq '.[0].databaseId') --exit-status`
Expected: exit 0, and new rows visible in the Notion database

---

### Task 11: Delete the Sheets modules

Separate commit, so the revert is one `git revert` if Notion disappoints.

**Files:**
- Delete: `sheets.py`, `styling.py`, `linkcheck.py`
- Modify: `internship_tracker.py` (drop the Sheets branch, `--no-style`, `--skip-links`, `--force-links`), `config.py` (drop `LISTINGS_HEADERS`, `PROGRAMS_HEADERS`, `REMOVED_*`, `LINK_CHECK_INTERVAL_DAYS`), `requirements.txt` (drop `gspread`, `google-auth`, `openpyxl`), `README.md`
- Create: `docs/sheet-archive-2026-09-24.csv`

- [ ] **Step 1: Export the sheet one last time**

```bash
set -a && source .env && set +a
./venv/bin/python -c "
import sys, csv; sys.path.insert(0,'.')
import config, sheets
ss = sheets.connect()
with open('docs/sheet-archive-2026-09-24.csv','w',newline='') as fh:
    w = csv.writer(fh)
    for tab in (config.LISTINGS_TAB, config.PROGRAMS_TAB):
        w.writerow([f'--- {tab} ---'])
        w.writerows(ss.worksheet(tab).get_all_values())
print('archived')"
```

- [ ] **Step 2: Delete and prune**

```bash
git rm sheets.py styling.py linkcheck.py
```

Then remove every reference the deletion breaks. Find them with:

Run: `grep -rn "sheets\.\|styling\.\|linkcheck\.\|--no-style\|--skip-links\|--force-links" *.py`
Expected after editing: no matches

- [ ] **Step 3: Run the tests**

Run: `./venv/bin/python -m unittest discover -s tests -v`
Expected: PASS

- [ ] **Step 4: Verify the pipeline still runs**

Run: `set -a && source .env && set +a && ./venv/bin/python internship_tracker.py --notion 2>&1 | tail -5`
Expected: no traceback, row count reported

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: remove the Google Sheets sink

The Notion database replaces it. A final CSV export of both tabs is
committed under docs/ and the sheet stays readable in Drive."
git push
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: architecture and module split to Tasks 1-5, deletion handling to Task 6, data flow to Task 7, posting time to Tasks 3 and 8, programs to Task 8, error handling to Tasks 2, 4 and 5, testing throughout, rollout to Tasks 7, 10 and 11. The schema table maps to Task 5's `SCHEMA` test.

**Placeholders.** None. Task 3 and Task 8 reference Maia's file by path and line range rather than reproducing 300 lines, and both list the exact rules that must survive the port, which is the part a reviewer would otherwise have to guess.

**Type consistency.** `job_id` is a `str` everywhere (Task 1 defines it, Tasks 5-8 consume it). `PageData` is defined in Task 2 and used in Tasks 3-4. `write_to_notion(api, database_id, listings, tombstoned)` has the same signature in Task 7's test and implementation. `enrich_all` mutates in place and returns `None` in both its definition and its call site.

**One risk worth naming.** Task 1 makes `job_id` depend on `sources.url_fingerprint`, and `sources` imports `parser`. The lazy import inside the property avoids the cycle, which is why it is written that way rather than at module level.
