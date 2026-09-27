"""Fill in the fields the Notion row wants, from the posting's own page.

Nothing here calls a model. A page that reads gives the employer's own
requirement text; a page that does not gives "See posting" and keywords
inferred from the role, and the cell says so.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
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


def keywords_for_role(role_title):
    """Keywords for a role, derived from its title alone. No fetch needed."""
    category = "Game Programming" if config.is_game_role(role_title) else "Software Engineering"
    return list(KEYWORDS_BY_CATEGORY.get(category, []))


def category_for(listing):
    """Which Notion Category select this listing belongs in."""
    return "Game Programming" if listing.is_game else "Software Engineering"


def _enrich_one(listing, session):
    listing.category = category_for(listing)
    listing.resume_keywords = list(KEYWORDS_BY_CATEGORY.get(listing.category, []))

    # A source that already returned the posting body (studio boards do)
    # saves a fetch and works where the vanity URL is a JavaScript shell.
    if listing.description:
        page = ats.PageData(text=listing.description, ok=True)
    else:
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
                # _enrich_one may have died before setting category/keywords at all
                # (e.g. category_for itself raised); a failed listing must still
                # carry the same three non-empty fields a successful one does.
                if not listing.category:
                    # category_for is what raised, so read the plain bool instead
                    # of calling it again on the same input.
                    listing.category = ("Game Programming" if listing.from_game_studio
                                        else "Software Engineering")
                if not listing.resume_keywords:
                    listing.resume_keywords = list(KEYWORDS_BY_CATEGORY.get(listing.category, []))
                if not listing.skills:
                    listing.skills = [GENERIC_SKILLS]
    readable = sum(1 for l in listings if l.skills != [GENERIC_SKILLS])
    log.info("enriched %d/%d listing(s) from their posting page", readable, len(listings))


def stamp_first_seen(listings, now=None):
    """Third rung of the posting-date ladder: date a listing to the run that saw it.

    Only fills a gap. A scraped or commit estimate always wins, and without
    this a listing whose page could not be read reaches Notion with a blank
    Posted, which blanks the Hours Since Posted column the Recent view sorts on.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    for listing in listings:
        if not listing.posted_at:
            listing.posted_at, listing.posted_precision = now, "first_seen"
