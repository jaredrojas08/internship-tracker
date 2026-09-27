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
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return ap.parse_args(argv)


def write_to_notion(api, database_id, listings, tombstoned):
    """Append listings Notion does not already hold. Never rewrites a page.

    The "nothing to do" short-circuit is keyed on Notion dedup alone: a batch
    that is new to Notion but entirely tombstoned still reaches add_all with
    an empty list, rather than skipping the call outright.

    Returns the listings add_all actually wrote, not merely attempted: the
    digest announces "new" listings from this, and a failed write must never
    be announced as if it landed.
    """
    seen = api.existing_job_ids(database_id)
    new = [l for l in listings if l.job_id not in seen]
    if not new:
        log.info("0 new listing(s) for Notion (%d already there)", len(listings))
        return []
    fresh = [l for l in new if l.job_id not in tombstoned]
    log.info("%d listing(s) already in Notion, %d tombstoned, %d new",
             len(listings) - len(new), len(new) - len(fresh), len(fresh))
    return api.add_all(database_id, fresh)


def backfill(api, database_id, limit=25):
    """Repair existing rows whose posting page was unreachable on an earlier run.

    Only Skill Requirements and Recruiter Contact are ever recovered, and only
    the ones a row is actually missing. A row that still can't be read, or
    whose posting still has nothing usable, is left exactly as it was.
    """
    rows = api.rows_to_backfill(database_id, limit)
    session = requests.Session()
    repaired = 0
    for row in rows:
        try:
            page = ats.fetch_page(row["url"], session)
            if not page.ok:
                continue
            skills = ats.extract_requirements(page.text) if row["needs_skills"] else ""
            recruiter = ""
            if row["needs_recruiter"]:
                markup = page.html or page.text
                recruiter = ats.extract_contact_email(markup)
            if not skills and not recruiter:
                continue
            api.update_row(row["page_id"], skills=skills, recruiter=recruiter)
            repaired += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not lose the rest
            log.error("failed to backfill %s (%s): %s", row.get("company", "?"),
                      row.get("page_id", "?"), exc)
    log.info("backfilled %d/%d flagged row(s)", repaired, len(rows))
    return repaired


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

    if args.create_database:
        api = notion_sink.Notion(config.require_env("NOTION_TOKEN"))
        print(api.create_database(config.require_env("NOTION_PARENT_PAGE_ID"),
                                  "Internship Listings"))
        return 0

    if not args.notion:
        log.error("Nothing to do: pass --notion, --create-database, or --test-notify.")
        return 1

    # 1. Fetch and parse every configured source.
    source_health = []
    try:
        listings = sources.fetch_all(health=source_health)
    except md_parser.ParseError as exc:
        log.error("Parse failed, refusing to write bad data: %s", exc)
        return 1
    if not listings:
        log.error("No listings from any source; refusing to wipe the database.")
        return 1

    listings, duplicates = sources.deduplicate(listings)
    log.info("%d unique listing(s) after removing %d duplicate(s)", len(listings), duplicates)

    enrich.enrich_all(listings)
    api = notion_sink.Notion(config.require_env("NOTION_TOKEN"))
    database_id = config.require_env("NOTION_DATABASE_ID")
    api.ensure_schema(database_id)
    new_listings = write_to_notion(api, database_id, listings, tombstones.load())
    # Rows whose page was unreachable on an earlier run carry "See posting".
    # Re-fetch a capped batch of them so a transient failure heals itself.
    backfill(api, database_id, limit=25)
    # Stamp before the digest is built, so a row marked Applied this run
    # already carries its date when the follow-up section is computed.
    stamp_applied_dates(api, database_id)
    # No per-source history tracked for Notion, so only outright failures and
    # zero-count sources surface; a slow shrink needs prior counts, and
    # nothing records those any more.
    health_warnings = sources.assess_health(source_health, {})
    if not args.no_notify:
        rows, total_count, applied_count = api.rows_for_digest(database_id)
        notify.send_digest_if_due(
            rows, new_listings, [], warnings=health_warnings,
            totals=(total_count, applied_count),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
