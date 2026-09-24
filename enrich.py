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
