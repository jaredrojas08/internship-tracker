"""Markdown table parsing for the summer-2027-internships README."""

import logging
import re
from dataclasses import dataclass

import requests

import config

log = logging.getLogger(__name__)

CLOSED_FLAG = "🔒"

MARKDOWN_LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<url>[^)]+)\)")


class ParseError(Exception):
    """Raised when the README shape has drifted far enough that we should stop."""


@dataclass
class Listing:
    company: str
    role: str
    location: str
    apply_url: str
    source_added: str = ""  # the repo's own "Added" date, often "-"

    @property
    def is_remote(self):
        return "remote" in self.location.lower()

    @property
    def key(self):
        """Identity across runs. Row position is never used for this."""
        return (
            self.company.strip().lower(),
            self.role.strip().lower(),
            self.apply_url.strip().lower(),
        )


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

    headers, rows = None, []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if headers is not None and rows:
                break  # table ended
            continue
        cells = _split_row(stripped)
        if headers is None:
            headers = cells
            continue
        if _is_separator(cells):
            continue
        rows.append(cells)

    if headers is None:
        log.warning("No table found under '## %s'.", heading)
        return None, []
    return headers, rows


def _column_index(headers, *candidates):
    """Find a column by name, case-insensitively. Returns None if absent."""
    lowered = [h.strip().lower() for h in headers]
    for candidate in candidates:
        if candidate in lowered:
            return lowered.index(candidate)
    return None


def extract_url(cell):
    """Pull the URL out of a markdown link cell, or return a bare URL as-is."""
    match = MARKDOWN_LINK.search(cell)
    if match:
        return match.group("url").strip()
    if cell.startswith("http"):
        return cell.strip()
    return ""


def strip_links(cell):
    """Replace markdown links with their visible text."""
    return MARKDOWN_LINK.sub(lambda m: m.group("text"), cell).strip()


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
        if not config.matches_role_filter(strip_links(role)):
            filtered += 1
            continue

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
