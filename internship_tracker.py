#!/usr/bin/env python3
"""Fetch Summer 2027 internship listings, filter them, and sync to Notion.

Run hourly by GitHub Actions.
"""

import argparse
import logging
import sys

import requests

import ats
import config
import enrich
import notify
import notion_sink
import parser as md_parser
import sources
import studios
import tombstones

log = logging.getLogger("internship_tracker")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--test-notify",
        action="store_true",
        help="send a sample digest to the configured channel and exit",
    )
    ap.add_argument(
        "--no-notify",
        action="store_true",
        help="skip the daily digest even if a channel is configured",
    )
    ap.add_argument("--notion", action="store_true",
                    help="run the pipeline and write to the Notion database")
    ap.add_argument("--create-database", action="store_true",
                    help="create the Notion database under NOTION_PARENT_PAGE_ID and print its id")
    ap.add_argument("--tombstone", metavar="JOB_ID", nargs="+",
                    help="record job ids deleted from Notion so they are never re-added")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return ap.parse_args(argv)


def _jobright_copy(listing, existing_titles):
    """True when this listing and an existing row are the same posting via jobright.

    jobright links never match an employer link, so that pair matches on company
    and title. Other pairs keep matching on job id only: Raytheon posts one title
    per city, and those are separate applications.
    """
    fingerprint = sources.title_fingerprint(listing.company, listing.role)
    if not fingerprint:
        return False
    company, role = fingerprint
    for (other_company, other_role), other_source in existing_titles.items():
        if other_role != role or "jobright" not in (listing.source, other_source):
            continue
        # jobright writes "Electronic Arts (EA)" for the board's "Electronic Arts".
        if company.startswith(other_company) or other_company.startswith(company):
            return True
    return False


def select_new(listings, existing, tombstoned, existing_titles=None):
    """Listings Notion does not already hold and the user has not tombstoned.

    Runs before enrichment, not after. Enriching first meant fetching all 735
    postings every hour to write nothing, which is both a month's Actions
    budget and half a million requests at other people's ATS servers.
    """
    new = [l for l in listings if l.job_id not in existing]
    new = [l for l in new if not _jobright_copy(l, existing_titles or {})]
    fresh = [l for l in new if l.job_id not in tombstoned]
    log.info("%d listing(s) already in Notion, %d tombstoned, %d new",
             len(listings) - len(new), len(new) - len(fresh), len(fresh))
    return fresh


def backfill(api, database_id, limit=25):
    """Repair existing rows whose posting page was unreachable on an earlier run.

    A page that still cannot be read gets "See posting" written into Skill
    Requirements. That is honest, and it takes the row out of the queue so an
    unreadable listing cannot hold the backlog behind it forever.
    """
    rows = api.rows_to_backfill(database_id, limit)
    session = requests.Session()
    repaired = 0
    for row in rows:
        try:
            page = ats.fetch_page(row["url"], session)
            if not page.ok:
                api.update_row(row["page_id"], skills=enrich.GENERIC_SKILLS)
                continue
            markup = page.html or page.text
            fields = {"skills": ats.extract_requirements(page.text) or enrich.GENERIC_SKILLS}
            if row.get("needs_recruiter"):
                fields["recruiter"] = ats.extract_contact_email(markup)
            if row.get("needs_notes"):
                fields["notes"] = ats.extract_notes(page.text)
            api.update_row(row["page_id"], **fields)
            repaired += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not lose the rest
            log.error("backfill failed for %s (%s): %s", row.get("company", ""), row["page_id"], exc)
    log.info("backfilled %d/%d flagged row(s)", repaired, len(rows))
    return repaired


def fill_keywords(api, database_id, limit=200):
    """Give rows their resume keywords, which need no page fetch.

    Keywords come from the role's category, so every row can have them
    whether or not its posting page can be read.
    """
    filled = 0
    for row in api.rows_to_backfill(database_id, limit=0):
        if not row.get("needs_keywords"):
            continue
        words = enrich.keywords_for_role(row.get("role", ""))
        if not words:
            continue
        try:
            api.update_row(row["page_id"], keywords=words)
            filled += 1
        except Exception as exc:  # noqa: BLE001
            log.error("keyword fill failed for %s: %s", row["page_id"], exc)
        if filled >= limit:
            break
    log.info("filled keywords on %d row(s)", filled)
    return filled

def stamp_applied_dates(api, database_id, as_of=None):
    """Record when a row was first seen as Applied. Never rewritten after that.

    Matches the Sheets version's behaviour: flipping Applied back and forth by
    accident must not destroy the record of when the application went out.
    """
    as_of = as_of or config.today()
    rows = api.rows_missing_applied_date(database_id)
    for row in rows:
        api.stamp_applied_date(row["page_id"], as_of.isoformat())
    log.info("stamped Applied Date on %d row(s)", len(rows))
    return len(rows)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    if args.test_notify:
        return 0 if notify.send_test() else 1

    if args.tombstone:
        added = tombstones.add(args.tombstone)
        print(f"{added} new tombstone(s); {len(tombstones.load())} total in "
              f"{tombstones.DEFAULT_PATH}")
        return 0

    if args.create_database:
        api = notion_sink.Notion(config.require_env("NOTION_TOKEN"))
        print(api.create_database(config.require_env("NOTION_PARENT_PAGE_ID"),
                                  "Internship Listings"))
        return 0

    if not args.notion:
        log.error("Nothing to do: pass --notion, --create-database, or --test-notify.")
        return 1

    # 1. Fetch and parse every configured source, sharing one download cache
    #    so the programs table doesn't refetch a README already pulled.
    downloads = {}
    source_health = []
    try:
        listings = sources.fetch_all(cache=downloads, health=source_health)
    except md_parser.ParseError as exc:
        log.error("Parse failed, refusing to write bad data: %s", exc)
        return 1
    if not listings:
        log.error("No listings from any source; refusing to wipe the database.")
        return 1

    listings, duplicates = sources.deduplicate(listings)
    log.info("%d unique listing(s) after removing %d duplicate(s)", len(listings), duplicates)

    programs = []
    try:
        markdown = downloads.get(config.README_URL) or md_parser.fetch_readme()
        programs = md_parser.programs_to_listings(md_parser.parse_programs(markdown))
    except requests.RequestException as exc:
        log.warning("Could not fetch the programs source; skipping programs this run: %s", exc)

    # No per-source history tracked for Notion, so only outright failures and
    # zero-count sources surface; a slow shrink needs prior counts, and
    # nothing records those any more.
    health_warnings = sources.assess_health(source_health, {})

    try:
        api = notion_sink.Notion(config.require_env("NOTION_TOKEN"))
        database_id = config.require_env("NOTION_DATABASE_ID")
        api.ensure_schema(database_id)
        existing = api.existing_job_ids(database_id)
        existing_titles = api.existing_title_fingerprints(database_id)
        tombstoned = tombstones.load()

        # Only new postings are fetched, so each one is fetched exactly once
        # over its lifetime rather than once an hour for as long as it is up.
        fresh = select_new(listings, existing, tombstoned, existing_titles)
        enrich.enrich_all(fresh)

        # Programs skip enrichment: it re-derives category from is_game and
        # re-fetches the apply page, which would overwrite "Program /
        # Fellowship" and the notes programs_to_listings just set.
        fresh = fresh + select_new(programs, existing, tombstoned)
        enrich.stamp_first_seen(fresh)

        new_listings = api.add_all(database_id, fresh)
        # Rows whose page was unreachable on an earlier run carry "See posting".
        # Re-fetch a capped batch of them so a transient failure heals itself.
        backfill(api, database_id, limit=25)
        # Keywords come from the role title, so every row can have them
        # without opening a page.
        fill_keywords(api, database_id, limit=200)
        # Stamp before the digest is built, so a row marked Applied this run
        # already carries its date when the follow-up section is computed.
        stamp_applied_dates(api, database_id)
        digest_rows = api.rows_for_digest(database_id) if not args.no_notify else None
    except Exception as exc:  # noqa: BLE001 - the alert is the whole point
        log.error("Notion sync failed: %s", exc)
        notify.send("Internship tracker: Notion sync failed",
                    f"🚨 **The run stopped before writing.**\n{exc}")
        return 1

    if digest_rows is not None:
        rows, total_count, applied_count = digest_rows
        notify.send_digest_if_due(
            rows, new_listings, [], warnings=health_warnings,
            totals=(total_count, applied_count),
            board_problems=studios.board_problems,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
