#!/usr/bin/env python3
"""One-time migration: read the Google Sheet and write every row into Notion.

Run with --dry-run first; it reads the sheet and reports counts without ever
touching Notion. A real run is idempotent (Job ID dedup), so it can be re-run
after a partial failure.
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

import config
import enrich
import notion_sink
import parser as md_parser
import sheets

log = logging.getLogger("migrate")

# The sheet spells "Not Applied" with a capital A; Notion's select option is
# "Not applied". An unmapped value would 400 the whole page write, so anything
# unrecognized falls back to the safe default instead of guessing.
APPLIED_LABELS = {
    "Not Applied": "Not applied",
    "Applying": "Applying",
    "Applied": "Applied",
}


def _applied_label(raw):
    return APPLIED_LABELS.get((raw or "").strip(), "Not applied")


def _first_seen(date_added):
    """Turn a plain "Date Added" cell into a tz-aware Posted estimate.

    The sheet only ever recorded a calendar date, so precision stays at
    "first_seen" -- the same bucket the daily pipeline uses for that case.
    """
    date_added = (date_added or "").strip()
    if not date_added:
        return None, "unknown"
    try:
        parsed = datetime.strptime(date_added, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None, "unknown"
    return parsed, "first_seen"


def _iso_date(value):
    """Return value if it parses as an ISO calendar date, else None.

    Several hand-typed Deadline cells read "rolling" or "check site" instead of
    a date. Notion's date property requires ISO 8601, and a free-text value
    sent through unchecked 400s the whole page write.
    """
    value = (value or "").strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def rows_to_listings(rows):
    """Convert Internship Listings rows (sheet column names, not sheets.py's
    reshaped keys) into Listing objects ready for notion_sink.

    Job ID is the dedup key and is derived from apply_url on the Listing
    itself, never carried from the sheet.
    """
    listings = []
    for row in rows:
        if row.get("Remove?", "") in sheets.TRUTHY:
            continue

        company = row.get("Company", "").strip()
        role = row.get("Role", "").strip()
        apply_url = row.get("Apply Link", "").strip()
        if not (company or role or apply_url):
            continue

        posted_at, precision = _first_seen(row.get("Date Added", ""))
        applied_date = row.get("Applied Date", "").strip()
        raw_deadline = row.get("Deadline", "").strip()
        deadline = _iso_date(raw_deadline) or ""

        # Notion has no separate applied-date column, and a non-ISO deadline has
        # no date column to go in either; both fold into Notes when present.
        notes_parts = []
        if applied_date:
            notes_parts.append(f"Applied {applied_date}")
        if raw_deadline and not deadline:
            notes_parts.append(f"Deadline: {raw_deadline}")

        listing = md_parser.Listing(
            company=company,
            role=role,
            location=row.get("Location", "").strip(),
            apply_url=apply_url,
            source=row.get("Source", "").strip(),
            salary=row.get("Salary", "").strip(),
            from_game_studio=row.get("Game?", "").strip().upper() == "YES",
            deadline=deadline,
            posted_at=posted_at,
            posted_precision=precision,
            notes="; ".join(notes_parts),
            applied=_applied_label(row.get("Application", "")),
        )
        listing.category = enrich.category_for(listing)
        listings.append(listing)
    return listings


def programs_to_listings(rows):
    """Programs & Fellowships rows folded into the same Listing shape.

    Per the spec: Organization -> Company, Opportunity -> Title, and the
    program's Type into Notes, all tagged with Category "Program / Fellowship".
    """
    listings = []
    for row in rows:
        if row.get("Remove?", "") in sheets.TRUTHY:
            continue

        org = row.get("Organization", "").strip()
        opportunity = row.get("Opportunity", "").strip()
        link = row.get("Link", "").strip()
        if not (org or opportunity or link):
            continue

        posted_at, precision = _first_seen(row.get("Date Added", ""))
        raw_deadline = row.get("Deadline", "").strip()
        deadline = _iso_date(raw_deadline) or ""
        type_ = row.get("Type", "").strip()

        notes_parts = []
        if type_:
            notes_parts.append(type_)
        if raw_deadline and not deadline:
            notes_parts.append(f"Deadline: {raw_deadline}")

        listing = md_parser.Listing(
            company=org,
            role=opportunity,
            location="",
            apply_url=link,
            source="programs",
            deadline=deadline,
            posted_at=posted_at,
            posted_precision=precision,
            notes="; ".join(notes_parts),
            applied="Applied" if row.get("Applied?", "") in sheets.TRUTHY else "Not applied",
        )
        listing.category = "Program / Fellowship"
        listings.append(listing)
    return listings


def _sheet_rows(worksheet):
    """Zip the header row onto every data row so keys match the sheet's own columns.

    Deliberately not sheets.read_listing_state, which reshapes keys to
    lowercase names like apply_url that would not match here.
    """
    values = worksheet.get_all_values()
    if len(values) < 2:
        return []
    headers = values[0]
    return [dict(zip(headers, row)) for row in values[1:]]


def write_all(api, database_id, listings, seen):
    """Create a page for each listing Notion does not already hold.

    `seen` is updated as rows are written, not just read once up front, so two
    rows sharing a job_id in the same batch are not both written. One bad row
    must not lose the rest, so a single failure is logged and skipped rather
    than aborting the batch.
    """
    written = 0
    for listing in listings:
        if listing.job_id in seen:
            continue
        try:
            api.add(database_id, listing)
            written += 1
            seen.add(listing.job_id)
        except Exception as exc:  # noqa: BLE001 - one bad row must not lose the rest
            log.error("failed to migrate %r (%s): %s", listing.role[:50], listing.job_id, exc)
        time.sleep(0.35)  # stay under Notion's ~3 req/s ceiling
    log.info("migrated %d/%d row(s)", written, len(listings))
    return written


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="read the sheet and report counts without writing to Notion",
    )
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    spreadsheet = sheets.connect()
    listings_ws = spreadsheet.worksheet(config.LISTINGS_TAB)
    programs_ws = spreadsheet.worksheet(config.PROGRAMS_TAB)

    listings = rows_to_listings(_sheet_rows(listings_ws))
    programs = programs_to_listings(_sheet_rows(programs_ws))

    if args.dry_run:
        log.info("DRY RUN -- nothing will be written to Notion.")
        log.info("%d listing(s) and %d program(s) would be migrated", len(listings), len(programs))
        return 0

    database_id = config.require_env("NOTION_DATABASE_ID")
    api = notion_sink.Notion(config.require_env("NOTION_TOKEN"))
    api.ensure_schema(database_id)
    seen = api.existing_job_ids(database_id)

    written_listings = write_all(api, database_id, listings, seen)
    written_programs = write_all(api, database_id, programs, seen)
    log.info("migrated %d listing(s) and %d program(s)", written_listings, written_programs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
