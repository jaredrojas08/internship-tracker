#!/usr/bin/env python3
"""Fetch Summer 2027 internship listings, filter them, and sync to Google Sheets.

Run daily by GitHub Actions. Use --dry-run to preview without writing.
"""

import argparse
import logging
import sys

import requests

import config
import linkcheck
import parser as md_parser
import sheets
import sources
import styling

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
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return ap.parse_args(argv)


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

    # 1. Fetch and parse every configured source, sharing one download cache
    #    so the programs table doesn't refetch a README already pulled.
    downloads = {}
    try:
        listings = sources.fetch_all(cache=downloads)
    except md_parser.ParseError as exc:
        log.error("Parse failed, refusing to write bad data: %s", exc)
        return 1
    if not listings:
        log.error("No listings from any source; refusing to wipe the sheet.")
        return 1

    listings, duplicates = sources.deduplicate(listings)
    log.info("%d unique listing(s) after removing %d duplicate(s)", len(listings), duplicates)

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
