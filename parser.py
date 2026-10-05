"""Markdown table parsing for the summer-2027-internships README."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import requests

import config

log = logging.getLogger(__name__)

CLOSED_FLAG = "🔒"

MARKDOWN_LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<url>[^)]+)\)")
# Several sources wrap the apply link in an HTML anchor around a badge image
# rather than using markdown link syntax.
HTML_ANCHOR = re.compile(r"""<a\s[^>]*href=["'](?P<url>[^"']+)["']""", re.IGNORECASE)
HTML_TAG = re.compile(r"<[^>]+>")


class ParseError(Exception):
    """Raised when the README shape has drifted far enough that we should stop."""


@dataclass
class Listing:
    company: str
    role: str
    location: str
    apply_url: str
    source_added: str = ""  # the repo's own "Added" date, often "-"
    source: str = ""  # which upstream list this came from
    salary: str = ""  # only some sources publish this
    # Set for postings that came from a game studio's own job board. A role
    # titled "Software Engineer Intern" at Riot is game work regardless of
    # whether the title contains a game keyword.
    from_game_studio: bool = False
    # No filter rule recognised the title. Kept and flagged rather than dropped,
    # since a silent drop hid every Activision internship for 26 days.
    needs_review: bool = False

    # --- Notion-side fields, filled in by enrich.py -------------------------
    posted_at: Optional[datetime] = None   # always tz-aware UTC
    # How much to trust posted_at: "scraped" | "commit" | "first_seen" | "day"
    posted_precision: str = "unknown"
    category: str = ""
    resume_keywords: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    recruiter: str = ""
    notes: str = ""
    deadline: str = ""            # ISO date, user-owned once written
    # Set by a source that already has the posting text, so enrich.py does
    # not refetch a page the listing call already returned.
    description: str = ""
    applied: str = ""             # Notion's Applied select name; "" means "Not applied"

    @property
    def is_remote(self):
        return "remote" in self.location.lower()

    @property
    def is_game(self):
        return self.from_game_studio or config.is_game_role(self.role)

    @property
    def term(self):
        return config.term_for(self.role)

    @property
    def key(self):
        """Identity across runs. Row position is never used for this."""
        return (
            self.company.strip().lower(),
            self.role.strip().lower(),
            self.apply_url.strip().lower(),
        )

    @property
    def job_id(self):
        """Stable identity for Notion dedup, derived from the posting URL."""
        import sources
        # Company keeps this branch from colliding across companies with the same
        # role text and no URL, since url_fingerprint returns None for that case.
        fallback = ("", f"{self.company.lower()}:{self.role.lower()}")
        host, ident = sources.url_fingerprint(self.apply_url) or fallback
        return f"{host}:{ident}"

    def skills_cell(self):
        return ", ".join(self.skills)


@dataclass
class Program:
    org: str
    opportunity: str
    link: str = ""
    type: str = ""
    deadline: str = ""

    @property
    def key(self):
        return (self.org.strip().lower(), self.opportunity.strip().lower())


def fetch_readme(url=config.README_URL):
    """Download the raw README. Raises on network failure."""
    log.info("Fetching %s", url)
    response = requests.get(url, timeout=config.REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def _split_row(line):
    """Split a markdown table row into cells, dropping the outer empty edges."""
    parts = line.strip().split("|")
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return [p.strip() for p in parts]


def _is_separator(cells):
    return bool(cells) and all(set(c) <= set("-: ") and "-" in c for c in cells)


def extract_section_table(markdown, heading):
    """Return (headers, rows) for the first markdown table under a '## heading'.

    Returns (None, []) if the heading or its table is missing, so a partial
    README change degrades instead of crashing the run.
    """
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(markdown)
    if not match:
        log.warning("Heading '## %s' not found — source format may have changed.", heading)
        return None, []

    body = markdown[match.end() :]
    next_heading = re.search(r"^##\s+", body, re.MULTILINE)
    if next_heading:
        body = body[: next_heading.start()]

    tables = _split_tables(body)
    if not tables:
        log.warning("No table found under '## %s'.", heading)
        return None, []

    headers, rows = tables[0]
    for other_headers, other_rows in tables[1:]:
        if other_headers == headers:
            rows = rows + other_rows
    return headers, rows


def extract_section_tables(markdown, heading):
    """Every table under a heading, each with its own header row.

    Sources that split one list into subsections don't necessarily keep the
    same columns: speedyapply's "Other" subsection omits the Salary column its
    FAANG+ subsection has. Collapsing them onto one header shifts every cell.
    """
    pattern = re.compile(rf"^#{{2,4}}\s+{re.escape(heading)}\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(markdown)
    if not match:
        log.warning("Heading %r not found.", heading)
        return []

    body = markdown[match.end() :]
    next_section = re.search(r"^##\s+(?!#)", body, re.MULTILINE)
    if next_section:
        body = body[: next_section.start()]
    return _split_tables(body)


def _split_tables(body):
    """Break a block of markdown into [(headers, rows), ...], one per table."""
    tables = []
    headers, rows = None, []

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = _split_row(stripped)
        if _is_separator(cells):
            continue
        # A row whose width differs, or that repeats header-like text, starts a
        # new table.
        if headers is None:
            headers, rows = cells, []
        elif len(cells) != len(headers) or cells == headers:
            if rows:
                tables.append((headers, rows))
            headers, rows = cells, []
        else:
            rows.append(cells)

    if headers is not None and rows:
        tables.append((headers, rows))
    return tables


def _column_index(headers, *candidates):
    """Find a column by name, case-insensitively. Returns None if absent."""
    lowered = [h.strip().lower() for h in headers]
    for candidate in candidates:
        if candidate in lowered:
            return lowered.index(candidate)
    return None


def extract_url(cell):
    """Pull the apply URL from a cell, whichever link syntax the source uses."""
    match = MARKDOWN_LINK.search(cell)
    if match:
        return match.group("url").strip()
    match = HTML_ANCHOR.search(cell)
    if match:
        return match.group("url").strip()
    stripped = cell.strip()
    if stripped.startswith("http"):
        return stripped
    return ""


def strip_links(cell):
    """Reduce a cell to its visible text, dropping markdown and HTML markup."""
    text = MARKDOWN_LINK.sub(lambda m: m.group("text"), cell)
    text = HTML_TAG.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_listings(markdown):
    """Parse '## the list' into Listing objects, excluding closed roles.

    Role emoji flags (🛂, 🇺🇸) are preserved deliberately; only 🔒 is dropped.
    """
    headers, rows = extract_section_table(markdown, "the list")
    if headers is None:
        raise ParseError("Could not locate the main listings table under '## the list'.")

    idx_company = _column_index(headers, "company")
    idx_role = _column_index(headers, "role")
    idx_location = _column_index(headers, "location")
    idx_apply = _column_index(headers, "apply")
    # The repo added an "Added" column after this project was specced. Optional
    # so the parser survives it being renamed or dropped again.
    idx_added = _column_index(headers, "added", "date added", "posted")

    missing = [
        name
        for name, idx in (
            ("Company", idx_company),
            ("Role", idx_role),
            ("Location", idx_location),
            ("Apply", idx_apply),
        )
        if idx is None
    ]
    if missing:
        raise ParseError(
            f"Listings table is missing expected column(s): {', '.join(missing)}. "
            f"Found headers: {headers}"
        )

    # The guard above already raised if any were missing; assert so the type
    # checker narrows them from int | None to int.
    assert idx_company is not None and idx_role is not None
    assert idx_location is not None and idx_apply is not None
    col_company, col_role = idx_company, idx_role
    col_location, col_apply = idx_location, idx_apply
    required_width = max(col_company, col_role, col_location, col_apply) + 1

    listings, closed, filtered, malformed = [], 0, 0, 0
    for cells in rows:
        if len(cells) < required_width:
            malformed += 1
            continue

        role = cells[col_role]
        if CLOSED_FLAG in role:
            closed += 1
            continue
        needs_review = False
        if not config.matches_role_filter(strip_links(role)):
            if not config.is_eligible(strip_links(role)):
                filtered += 1
                continue
            needs_review = True

        apply_url = extract_url(cells[col_apply])
        if not apply_url:
            malformed += 1
            continue

        source_added = ""
        if idx_added is not None and len(cells) > idx_added:
            raw_added = cells[idx_added].strip()
            source_added = "" if raw_added in {"-", "--", "—"} else raw_added

        listings.append(
            Listing(
                company=strip_links(cells[col_company]),
                role=role.strip(),
                location=strip_links(cells[col_location]),
                apply_url=apply_url,
                source_added=source_added,
                needs_review=needs_review,
            )
        )

    log.info(
        "Listings: %d kept, %d closed, %d filtered out, %d malformed (of %d rows)",
        len(listings),
        closed,
        filtered,
        malformed,
        len(rows),
    )
    if rows and not listings:
        log.warning("Every row was rejected — the README format may have changed.")
    return listings


def parse_programs(markdown):
    """Parse '## programs open now'. The URL lives inside the opportunity cell."""
    headers, rows = extract_section_table(markdown, "programs open now")
    if headers is None:
        log.warning("Skipping programs table — heading not found.")
        return []

    idx_org = _column_index(headers, "org", "organization")
    idx_opp = _column_index(headers, "opportunity")
    idx_type = _column_index(headers, "type")
    idx_deadline = _column_index(headers, "deadline")

    if idx_org is None or idx_opp is None:
        log.warning("Programs table missing org/opportunity columns: %s", headers)
        return []

    programs = []
    for cells in rows:
        if len(cells) <= max(idx_org, idx_opp):
            continue
        opportunity_cell = cells[idx_opp]
        programs.append(
            Program(
                org=strip_links(cells[idx_org]),
                opportunity=strip_links(opportunity_cell),
                link=extract_url(opportunity_cell),
                type=cells[idx_type] if idx_type is not None and len(cells) > idx_type else "",
                deadline=(
                    cells[idx_deadline]
                    if idx_deadline is not None and len(cells) > idx_deadline
                    else ""
                ),
            )
        )

    log.info("Programs: %d parsed", len(programs))
    return programs


def _iso_date(value):
    """Return value if it parses as an ISO calendar date, else None.

    Most Deadline cells in the programs table read "rolling" or "check site"
    instead of a date. Notion's date property requires ISO 8601, and a
    free-text value sent through unchecked 400s the whole page write.
    """
    value = (value or "").strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def programs_to_listings(programs):
    """Fold Programs & Fellowships rows into the same Listing shape as everything else.

    Org -> Company, Opportunity -> Title. There's no dedicated property for a
    program's Type, so it folds into Notes; a Deadline that isn't a real ISO
    date (most aren't) has nowhere else to go either, so it joins it there.
    """
    listings = []
    for program in programs:
        deadline = _iso_date(program.deadline) or ""

        notes_parts = []
        if program.type:
            notes_parts.append(program.type)
        if program.deadline and not deadline:
            notes_parts.append(f"Deadline: {program.deadline}")

        listing = Listing(
            company=program.org,
            role=program.opportunity,
            location="",
            apply_url=program.link,
            source="programs",
            deadline=deadline,
            notes="; ".join(notes_parts),
        )
        listing.category = "Program / Fellowship"
        listings.append(listing)
    return listings
