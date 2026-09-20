"""Upstream listing sources and cross-source deduplication.

Each source knows how to turn one upstream README into Listing objects. The
shapes differ enough (markdown links vs HTML anchors, different column names,
subsections) that each gets its own parse function rather than one generic one.

Adding a source: write a parse function, add it to SOURCES. Everything
downstream — filtering, dedup, sheet sync, link checking — is source-agnostic.
"""

import logging
import re
from urllib.parse import urlparse

import config
import parser as md
import studios

log = logging.getLogger(__name__)


# --- Source definitions ----------------------------------------------------


def parse_sndsh404(markdown):
    """sndsh404/summer-2027-internships — markdown links, single table."""
    listings = md.parse_listings(markdown)
    for listing in listings:
        listing.source = "sndsh404"
    return listings


def parse_speedyapply(markdown):
    """speedyapply/2027-SWE-College-Jobs.

    Columns: Company | Position | Location | Salary | Posting | Age
    The company cell is an anchor around the company website, the apply link is
    an anchor around a badge image, and rows are split across FAANG+/Quant/Other
    subsections beneath one h2.
    """
    tables = md.extract_section_tables(markdown, "2027 USA SWE Internships :books::eagle:")
    if not tables:
        log.warning("speedyapply: could not locate the internships table")
        return []

    listings, filtered, total = [], 0, 0
    for headers, rows in tables:
        total += len(rows)
        listings_from, filtered_from = _parse_speedy_table(headers, rows)
        listings.extend(listings_from)
        filtered += filtered_from

    log.info(
        "speedyapply: %d kept, %d filtered out (of %d rows in %d table(s))",
        len(listings),
        filtered,
        total,
        len(tables),
    )
    return listings


def _parse_speedy_table(headers, rows):
    """Parse one speedyapply subsection. Each has its own column layout."""
    idx_company = md._column_index(headers, "company")
    idx_role = md._column_index(headers, "position", "role")
    idx_location = md._column_index(headers, "location")
    idx_apply = md._column_index(headers, "posting", "apply", "application/link")
    idx_salary = md._column_index(headers, "salary")
    idx_age = md._column_index(headers, "age")

    if None in (idx_company, idx_role, idx_apply):
        log.warning("speedyapply: unexpected columns %s", headers)
        return [], 0

    listings, filtered = [], 0
    for cells in rows:
        if len(cells) <= max(idx_company, idx_role, idx_apply):
            continue
        role = md.strip_links(cells[idx_role])
        if md.CLOSED_FLAG in cells[idx_role] or not role:
            continue
        # Same relevance and eligibility rules as every other source.
        if not config.matches_role_filter(role):
            filtered += 1
            continue
        url = md.extract_url(cells[idx_apply])
        if not url:
            continue
        listings.append(
            md.Listing(
                company=md.strip_links(cells[idx_company]),
                role=role,
                location=md.strip_links(cells[idx_location])
                if idx_location is not None and len(cells) > idx_location
                else "",
                apply_url=url,
                source_added=_age_to_note(cells[idx_age])
                if idx_age is not None and len(cells) > idx_age
                else "",
                source="speedyapply",
                salary=md.strip_links(cells[idx_salary])
                if idx_salary is not None and len(cells) > idx_salary
                else "",
            )
        )

    return listings, filtered


def _age_to_note(cell):
    """'5d' -> '5d ago'. The source gives relative age, not a date."""
    value = md.strip_links(cell)
    return f"{value} ago" if re.fullmatch(r"\d+d", value) else ""



SOURCES = [
    {
        "name": "sndsh404",
        "url": config.README_URL,
        "parse": parse_sndsh404,
    },
    {
        "name": "speedyapply",
        "url": "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md",
        "parse": parse_speedyapply,
    },
    {
        # Not a markdown list: queries game studio ATS APIs directly.
        "name": "studios",
        "fetch": studios.fetch_studio_listings,
        # Studios post summer internships Sept–Jan. Empty for most of the year
        # is the calendar, not a breakage, so it must not raise a health alarm.
        "allow_empty": True,
    },
]


# --- Deduplication ---------------------------------------------------------

_JOB_ID = re.compile(r"\d{6,}")
_COMPANY_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|company|group|holdings|technologies|labs"
    r"|management|capital|partners)\b",
    re.IGNORECASE,
)


def url_fingerprint(url):
    """Identify the underlying job posting, ignoring URL cosmetics.

    The same Google role appears as .../results/8556471326124512 in one list and
    .../results/8556471326124512-software-engineering-intern/ in another, plus
    tracking params. Matching on the numeric job id collapses those.
    """
    if not url:
        return None
    cleaned = url.split("?")[0].split("#")[0].rstrip("/")
    parsed = urlparse(cleaned)
    host = parsed.netloc.lower().replace("www.", "")
    # Greenhouse serves the same board on both hostnames.
    host = re.sub(r"^(job-)?boards\.greenhouse\.io$", "greenhouse.io", host)
    ids = _JOB_ID.findall(parsed.path)
    if ids:
        return (host, ids[-1])
    return (host, parsed.path.lower())


def _normalize(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def title_fingerprint(company, role):
    """Fallback identity for postings whose URLs don't share an id.

    Deliberately conservative: only case, punctuation, whitespace and emoji are
    normalized away. Everything else is kept, because the distinguishing detail
    usually lives exactly where it is tempting to strip:

        Software Engineering Intern (Summer 2027)  vs  Co-op (Winter 2027)
        Software Engineer Intern (Austin)          vs  (Chicago)
        Software Engineer Intern (1)               vs  (2)

    Stripping seasons or parentheticals merged all of those. The costs are not
    symmetric: a false merge hides a real posting permanently, while a missed
    duplicate merely shows up twice, so this errs toward showing twice.

    Cross-source wording differences ("Intern, BS (Summer 2027)" vs
    "Intern - BS - Summer 2027") still collapse, since only punctuation differs.
    """
    company_key = _normalize(_COMPANY_SUFFIX.sub("", company))
    role_key = _normalize(role)
    if not company_key or not role_key:
        return None
    return (company_key, role_key)


def deduplicate(listings):
    """Collapse the same job appearing in more than one source.

    Earlier sources win, so SOURCES order is a priority order. Returns
    (unique_listings, duplicate_count).
    """
    seen_urls, seen_titles = set(), set()
    unique, duplicates = [], 0

    for listing in listings:
        url_key = url_fingerprint(listing.apply_url)
        title_key = title_fingerprint(listing.company, listing.role)

        if (url_key and url_key in seen_urls) or (title_key and title_key in seen_titles):
            duplicates += 1
            continue

        if url_key:
            seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        unique.append(listing)

    return unique, duplicates


def fetch_all(only=None, cache=None, health=None):
    """Fetch and parse every configured source. Failures degrade, not abort.

    `cache` is an optional {url: markdown} dict, filled in as sources are
    fetched, so a caller that also needs one of the READMEs (the programs table
    lives on sndsh404's) doesn't download it twice.

    `health` is an optional list that gets one entry per source describing
    whether it worked and how much it returned. Degrading quietly is correct —
    one broken upstream should not block the others — but it must not be
    *invisible*, or a source can disappear for weeks unnoticed.
    """
    cache = {} if cache is None else cache
    health = [] if health is None else health
    collected = []
    for source in SOURCES:
        if only and source["name"] not in only:
            continue
        try:
            if "fetch" in source:
                listings = source["fetch"]()
            else:
                if source["url"] not in cache:
                    cache[source["url"]] = md.fetch_readme(source["url"])
                listings = source["parse"](cache[source["url"]])
        except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
            log.warning("Source %r failed, continuing without it: %s", source["name"], exc)
            health.append(
                {
                    "name": source["name"],
                    "count": 0,
                    "ok": False,
                    "error": str(exc)[:120],
                    "allow_empty": source.get("allow_empty", False),
                }
            )
            continue
        log.info("Source %r: %d listing(s) after filtering", source["name"], len(listings))
        health.append(
            {
                "name": source["name"],
                "count": len(listings),
                "ok": True,
                "error": "",
                "allow_empty": source.get("allow_empty", False),
            }
        )
        collected.extend(listings)
    return collected


def retain_from_state(state, listings, failed_sources):
    """Rebuild listings for sources that failed, from what the sheet already has.

    Without this, one upstream 404 deletes every row that source contributed —
    a transient outage becomes permanent data loss, and the rows come back as
    "new" whenever the source recovers, resetting their Date Added. Retaining
    them keeps a broken source's listings visible and stable until it returns.
    """
    if not failed_sources:
        return []

    have = {listing.key for listing in listings}
    retained = []
    for existing in state.values():
        if existing.get("source") not in failed_sources:
            continue
        key = (
            existing["company"].strip().lower(),
            existing["role"].strip().lower(),
            existing["apply_url"].strip().lower(),
        )
        if key in have:
            continue
        retained.append(
            md.Listing(
                company=existing["company"],
                role=existing["role"],
                location=existing.get("location", ""),
                apply_url=existing["apply_url"],
                source=existing.get("source", ""),
                salary=existing.get("salary", ""),
            )
        )
    if retained:
        log.warning(
            "Retained %d listing(s) from failed source(s) %s rather than deleting them",
            len(retained),
            ", ".join(sorted(failed_sources)),
        )
    return retained


def failed_source_names(health):
    """Sources that errored or came back empty this run."""
    return {
        h["name"]
        for h in health
        if not h["ok"] or (h["count"] == 0 and not h.get("allow_empty"))
    }


def assess_health(health, prior_counts, drop_threshold=0.5):
    """Return human-readable warnings about sources that broke or shrank.

    `prior_counts` maps source name -> how many rows that source has on the
    sheet right now. A source that parses but returns far less than last time
    has usually had its upstream format changed underneath it, which is just as
    damaging as an outright failure and much easier to miss.
    """
    warnings = []
    for entry in health:
        name, count = entry["name"], entry["count"]
        if not entry["ok"]:
            warnings.append(f"{name}: FAILED ({entry['error']})")
            continue
        if count == 0:
            if entry.get("allow_empty"):
                continue  # expected to be empty out of season
            warnings.append(f"{name}: parsed 0 listings — upstream format probably changed")
            continue
        prior = prior_counts.get(name, 0)
        if prior and count < prior * drop_threshold:
            warnings.append(
                f"{name}: {count} listings, down from {prior} — upstream may have changed"
            )
    return warnings
