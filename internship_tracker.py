#!/usr/bin/env python3
"""Fetch Summer 2027 internship listings, filter them, and sync to Google Sheets.

Run daily by GitHub Actions. Use --dry-run to preview without writing.
"""

import argparse
import logging
import sys

import requests

import ats
import config
import enrich
import linkcheck
import notify
import notion_sink
import parser as md_parser
import sheets
import sources
import styling
import tombstones

log = logging.getLogger("internship_tracker")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would change without writing to the sheet",
    )
    ap.add_argument(
        "--no-style",
        action="store_true",
        help="skip the formatting pass (faster; useful when iterating)",
    )
    ap.add_argument(
        "--skip-links",
        action="store_true",
        help="skip checking whether application links are still live",
    )
    ap.add_argument(
        "--force-links",
        action="store_true",
        help="re-check every link now, ignoring the weekly interval",
    )
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
                    help="write to the Notion database instead of the sheet")
    ap.add_argument("--create-database", action="store_true",
                    help="create the Notion database under NOTION_PARENT_PAGE_ID and print its id")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return ap.parse_args(argv)


def write_to_notion(api, database_id, listings, tombstoned):
    """Append listings Notion does not already hold. Never rewrites a page.

    The "nothing to do" short-circuit is keyed on Notion dedup alone: a batch
    that is new to Notion but entirely tombstoned still reaches add_all with
    an empty list, rather than skipping the call outright.
    """
    seen = api.existing_job_ids(database_id)
    new = [l for l in listings if l.job_id not in seen]
    if not new:
        log.info("0 new listing(s) for Notion (%d already there)", len(listings))
        return 0
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
    log.info("backfilled %d/%d flagged row(s)", repaired, len(rows))
    return repaired


def report(new_listings, dropped, total, programs, rows=(), dropped_programs=()):
    log.info("-" * 60)
    log.info("%d listing(s) on the sheet after this run", total)
    log.info("%d program(s)", len(programs))

    stale = [r for r in rows if r.get("Link Status") in ("DEAD", "CLOSED")]
    if stale:
        log.info("%d listing(s) no longer applicable (sunk to bottom, not deleted):", len(stale))
        for row in stale:
            log.info("  ! [%s] %s — %s", row["Link Status"], row["Company"], row["Role"])

    if new_listings:
        log.info("%d NEW listing(s):", len(new_listings))
        for listing in new_listings:
            flag = "REMOTE" if listing.is_remote else "onsite"
            log.info("  + [%s] %s — %s", flag, listing.company, listing.role)
    else:
        log.info("No new listings.")

    if dropped:
        log.info("%d listing(s) removed via checkbox:", len(dropped))
        for listing in dropped:
            log.info("  - %s — %s", listing.company, listing.role)
    if dropped_programs:
        log.info("%d program(s) removed via checkbox:", len(dropped_programs))
        for program in dropped_programs:
            log.info("  - %s — %s", program.org, program.opportunity)
    log.info("-" * 60)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    if args.test_notify:
        return 0 if notify.send_test() else 1

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
        log.error("No listings from any source; refusing to wipe the sheet.")
        return 1

    listings, duplicates = sources.deduplicate(listings)
    log.info("%d unique listing(s) after removing %d duplicate(s)", len(listings), duplicates)

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
        # Rows whose page was unreachable on an earlier run carry "See posting".
        # Re-fetch a capped batch of them so a transient failure heals itself.
        backfill(api, database_id, limit=25)
        # The digest call lands in Task 9, which is where send_digest_if_due
        # is written. Adding it here would raise AttributeError.
        return 0

    # The programs table only exists on the original source.
    try:
        markdown = downloads.get(config.README_URL) or md_parser.fetch_readme()
    except requests.RequestException as exc:
        log.error("Could not fetch the programs source: %s", exc)
        return 1
    programs = md_parser.parse_programs(markdown)

    if args.dry_run and not _can_connect():
        # Still useful without credentials: show what the filter produced.
        log.warning("No credentials available — showing parsed results only.")
        for listing in listings[:25]:
            log.info("  [%s] %s — %s", "REMOTE" if listing.is_remote else "onsite",
                     listing.company, listing.role)
        log.info("(%d total)", len(listings))
        return 0

    # 2. Connect and make sure the tabs exist.
    try:
        spreadsheet = sheets.connect()
    except Exception as exc:  # noqa: BLE001 - surface any auth/API failure clearly
        log.error("Could not open the spreadsheet: %s", exc)
        return 1

    # A dry run must not touch the spreadsheet at all, including tab creation.
    ro = args.dry_run
    listings_ws = sheets.ensure_worksheet(
        spreadsheet, config.LISTINGS_TAB, config.LISTINGS_HEADERS, read_only=ro
    )
    programs_ws = sheets.ensure_worksheet(
        spreadsheet, config.PROGRAMS_TAB, config.PROGRAMS_HEADERS, read_only=ro
    )
    removed_ws = sheets.ensure_worksheet(
        spreadsheet, config.REMOVED_TAB, config.REMOVED_HEADERS, hidden=True, read_only=ro
    )
    removed_programs_ws = sheets.ensure_worksheet(
        spreadsheet,
        config.REMOVED_PROGRAMS_TAB,
        config.REMOVED_PROGRAMS_HEADERS,
        hidden=True,
        read_only=ro,
    )

    # 3. Read current state, keyed by listing identity rather than row number.
    state = sheets.read_listing_state(listings_ws)
    removed_keys = sheets.read_removed_keys(removed_ws)
    log.info("Sheet currently holds %d row(s), %d tombstoned", len(state), len(removed_keys))

    # 3b. Check links, but only ones never checked or checked over a week ago.
    # Everything else carries last week's verdict forward, so the daily run
    # stays fast and the job boards aren't hit 95 times a day.
    link_status = {}
    if not args.skip_links:
        due = [
            l
            for l in listings
            if args.force_links or sheets.needs_link_check(state.get(l.key))
        ]
        skipped = len(listings) - len(due)
        if skipped:
            log.info(
                "Re-checking %d link(s); %d checked within the last %d days",
                len(due),
                skipped,
                config.LINK_CHECK_INTERVAL_DAYS,
            )
        if due:
            link_status = linkcheck.check_all([l.apply_url for l in due])

    rows, new_listings, dropped = sheets.build_rows(
        listings, state, removed_keys, link_status=link_status
    )

    # A failed source must not delete the rows it contributed last run.
    failed = sources.failed_source_names(source_health)
    retained = sources.retain_from_state(state, listings, failed)
    if retained:
        listings = listings + retained
        rows, new_listings, dropped = sheets.build_rows(
            listings, state, removed_keys, link_status=link_status
        )

    # Compare each source against what it contributed last run, so a source
    # that quietly shrinks is as visible as one that outright fails.
    prior_counts = {}
    for existing in state.values():
        name = existing.get("source", "")
        if name:
            prior_counts[name] = prior_counts.get(name, 0) + 1
    health_warnings = sources.assess_health(source_health, prior_counts)
    for warning in health_warnings:
        log.warning("Source health — %s", warning)

    # 4. Programs: same user-owned checkbox handling as the listings tab.
    program_state = sheets.read_program_state(programs_ws)
    removed_program_keys = sheets.read_removed_program_keys(removed_programs_ws)
    program_values, dropped_programs = sheets.build_program_rows(
        programs, program_state, removed_program_keys
    )

    if args.dry_run:
        log.info("DRY RUN — nothing will be written.")
        report(new_listings, dropped, len(rows), programs, rows, dropped_programs)
        return 0

    # 5. Write. The table's column types must be aligned with the current
    #    headers first — see styling.sync_table_schema.
    sheets.append_removed(removed_ws, dropped)
    sheets.append_removed_programs(removed_programs_ws, dropped_programs)
    if not args.no_style:
        try:
            styling.sync_table_schema(
                spreadsheet, listings_ws, programs_ws, len(rows), len(program_values)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not sync table schema before write: %s", exc)
    sheets.write_block(listings_ws, config.LISTINGS_HEADERS, sheets.rows_to_values(rows))
    sheets.write_block(programs_ws, config.PROGRAMS_HEADERS, program_values)

    if not args.no_style:
        try:
            styling.apply_all(spreadsheet, listings_ws, programs_ws, len(rows), len(program_values))
        except Exception as exc:  # noqa: BLE001 - styling must never lose data
            log.warning("Formatting pass failed (data is written and safe): %s", exc)

    # 6. Digest. Runs last, after the sheet is safely written, and never
    #    fails the run — a missed notification is not worth losing data over.
    if not args.no_notify:
        digest = notify.build_digest(rows, new_listings, dropped, warnings=health_warnings)
        if digest:
            notify.send(*digest)
        else:
            log.info("Nothing actionable today; no digest sent.")

    report(new_listings, dropped, len(rows), programs, rows, dropped_programs)
    return 0


def _can_connect():
    try:
        config.load_credentials_info()
        config.get_sheet_id()
        return True
    except RuntimeError:
        return False



if __name__ == "__main__":
    sys.exit(main())
