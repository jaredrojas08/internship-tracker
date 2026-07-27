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
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return ap.parse_args(argv)


def report(new_listings, dropped, total, programs, rows=()):
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
    log.info("-" * 60)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    # 1. Fetch and parse the source.
    try:
        markdown = md_parser.fetch_readme()
    except requests.RequestException as exc:
        log.error("Could not fetch the source README: %s", exc)
        return 1

    try:
        listings = md_parser.parse_listings(markdown)
    except md_parser.ParseError as exc:
        log.error("Parse failed, refusing to write bad data: %s", exc)
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

    # 3. Read current state, keyed by listing identity rather than row number.
    state = sheets.read_listing_state(listings_ws)
    removed_keys = sheets.read_removed_keys(removed_ws)
    log.info("Sheet currently holds %d row(s), %d tombstoned", len(state), len(removed_keys))

    # 3b. Check whether each application link is still live and still open.
    link_status = {}
    if not args.skip_links:
        link_status = linkcheck.check_all([l.apply_url for l in listings])

    rows, new_listings, dropped = sheets.build_rows(
        listings, state, removed_keys, link_status=link_status
    )

    # 4. Programs: preserve the date each was first seen.
    today_str = sheets.today().isoformat()
    program_dates = _existing_program_dates(programs_ws)
    program_values = sheets.programs_to_values(programs, program_dates, today_str)

    if args.dry_run:
        log.info("DRY RUN — nothing will be written.")
        report(new_listings, dropped, len(rows), programs, rows)
        return 0

    # 5. Write.
    sheets.append_removed(removed_ws, dropped)
    sheets.write_block(listings_ws, config.LISTINGS_HEADERS, sheets.rows_to_values(rows))
    sheets.write_block(programs_ws, config.PROGRAMS_HEADERS, program_values)

    if not args.no_style:
        try:
            styling.apply_all(spreadsheet, listings_ws, programs_ws, len(rows), len(program_values))
        except Exception as exc:  # noqa: BLE001 - styling must never lose data
            log.warning("Formatting pass failed (data is written and safe): %s", exc)

    report(new_listings, dropped, len(rows), programs, rows)
    return 0


def _can_connect():
    try:
        config.load_credentials_info()
        config.get_sheet_id()
        return True
    except RuntimeError:
        return False


def _existing_program_dates(worksheet):
    """Map program key -> the Date Added already recorded for it."""
    if worksheet is None:
        return {}
    rows = worksheet.get_all_values()
    if len(rows) < 2:
        return {}
    dates = {}
    for row in rows[1:]:
        padded = row + [""] * len(config.PROGRAMS_HEADERS)
        org, opportunity, date_added = padded[0].strip(), padded[1].strip(), padded[5].strip()
        if org or opportunity:
            dates[(org.lower(), opportunity.lower())] = date_added or ""
    return {k: v for k, v in dates.items() if v}


if __name__ == "__main__":
    sys.exit(main())
