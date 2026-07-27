"""Google Sheets read/write.

Design note: row position carries no meaning. Every listing is identified by
(company, role, apply_url), and the user-owned columns are re-attached to that
key on each run. That is what lets the sheet be fully re-sorted every day
without ever detaching Jared's "Applied?" and "Remove?" checkboxes from the
listing they belong to.
"""

import datetime as dt
import logging

import gspread
from google.oauth2.service_account import Credentials

import config

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TRUTHY = {"TRUE", "true", "True", True, "YES", "yes", "1"}


def today():
    return dt.date.today()


def _parse_date(value):
    try:
        return dt.date.fromisoformat(str(value).strip())
    except (ValueError, AttributeError):
        return None


def connect():
    """Authorize and open the target spreadsheet."""
    creds = Credentials.from_service_account_info(
        config.load_credentials_info(), scopes=SCOPES
    )
    client = gspread.authorize(creds)
    return client.open_by_key(config.get_sheet_id())


def ensure_worksheet(spreadsheet, title, headers, hidden=False, read_only=False):
    """Get a worksheet by title, creating it with headers if absent.

    With read_only=True (dry runs) nothing is created or rewritten; a missing
    tab simply returns None so the caller can treat its state as empty.
    """
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        if read_only:
            log.info("Tab %r does not exist yet (would be created).", title)
            return None
        log.info("Creating tab %r", title)
        worksheet = spreadsheet.add_worksheet(
            title=title, rows=1000, cols=max(len(headers), 10)
        )
        worksheet.update(values=[headers], range_name="A1")
        if hidden:
            worksheet.hide()
        return worksheet

    existing = worksheet.row_values(1)
    if existing != headers and not read_only:
        log.info("Rewriting header row on %r", title)
        worksheet.update(values=[headers], range_name="A1")
    return worksheet


# --- Reading state ---------------------------------------------------------


def read_listing_state(worksheet):
    """Return {key: {...}} of what the sheet currently holds.

    The key is the same (company, role, apply_url) tuple Listing.key produces,
    so parsed listings and sheet rows can be matched without relying on order.
    """
    if worksheet is None:
        return {}
    rows = worksheet.get_all_values()
    if len(rows) < 2:
        return {}

    headers = rows[0]

    def col(name):
        return headers.index(name) if name in headers else None

    idx = {name: col(name) for name in config.LISTINGS_HEADERS}
    state = {}

    for row in rows[1:]:
        def cell(name):
            i = idx.get(name)
            return row[i].strip() if i is not None and i < len(row) else ""

        company, role, url = cell("Company"), cell("Role"), cell("Apply Link")
        if not (company or role or url):
            continue

        key = (company.lower(), role.lower(), url.lower())
        state[key] = {
            "company": company,
            "role": role,
            "location": cell("Location"),
            "apply_url": url,
            "date_added": cell("Date Added"),
            "applied": cell("Applied?") in TRUTHY,
            "remove": cell("Remove?") in TRUTHY,
        }
    return state


def read_removed_keys(worksheet):
    """Keys the user has previously removed. These must never be re-added."""
    if worksheet is None:
        return set()
    rows = worksheet.get_all_values()
    if len(rows) < 2:
        return set()
    removed = set()
    for row in rows[1:]:
        padded = row + [""] * 3
        company, role, url = (padded[0].strip(), padded[1].strip(), padded[2].strip())
        if company or role or url:
            removed.add((company.lower(), role.lower(), url.lower()))
    return removed


# --- Merge and sort --------------------------------------------------------


def status_for(date_added, as_of=None):
    """NEW for the first few days after the script first saw the listing."""
    as_of = as_of or today()
    added = _parse_date(date_added)
    if added is None:
        return "SEEN"
    return "NEW" if (as_of - added).days < config.NEW_STATUS_DAYS else "SEEN"


def build_rows(listings, state, removed_keys, as_of=None):
    """Merge parsed listings with sheet state. Returns (rows, newly_added, dropped).

    Precedence: the user's Remove? wins over everything. Their Applied? value is
    carried forward untouched. Date Added is preserved from the existing row so
    a listing's NEW window doesn't restart on every run.
    """
    as_of = as_of or today()
    today_str = as_of.isoformat()

    rows, newly_added, dropped = [], [], []

    for listing in listings:
        key = listing.key

        if key in removed_keys:
            continue  # tombstoned in a previous run

        existing = state.get(key)
        if existing and existing["remove"]:
            dropped.append(listing)  # removal requested this run
            continue

        if existing:
            date_added = existing["date_added"] or today_str
            applied = existing["applied"]
        else:
            date_added = today_str
            applied = False
            newly_added.append(listing)

        rows.append(
            {
                "listing": listing,
                "Company": listing.company,
                "Role": listing.role,
                "Location": listing.location,
                "Apply Link": listing.apply_url,
                "Date Added": date_added,
                "Remote?": "YES" if listing.is_remote else "NO",
                "Status": status_for(date_added, as_of),
                "Applied?": applied,
                "Remove?": False,
            }
        )

    rows.sort(key=_sort_key)
    return rows, newly_added, dropped


def _sort_key(row):
    """NEW first, then remote, then newest, then company.

    Fresh listings surface for three days regardless of location; after that the
    sheet settles into remote-at-top. Tuples sort ascending, so each component is
    expressed as "0 means first".
    """
    is_new = 0 if row["Status"] == "NEW" else 1
    is_remote = 0 if row["Remote?"] == "YES" else 1
    added = _parse_date(row["Date Added"])
    recency = -added.toordinal() if added else 0
    return (is_new, is_remote, recency, row["Company"].lower())


def rows_to_values(rows):
    """Flatten merged rows into the sheet's column order."""
    return [
        [
            row["Company"],
            row["Role"],
            row["Location"],
            row["Apply Link"],
            row["Date Added"],
            row["Remote?"],
            row["Status"],
            bool(row["Applied?"]),
            bool(row["Remove?"]),
        ]
        for row in rows
    ]


def programs_to_values(programs, state_dates, today_str):
    values = []
    for program in programs:
        values.append(
            [
                program.org,
                program.opportunity,
                program.link,
                program.type,
                program.deadline,
                state_dates.get(program.key, today_str),
            ]
        )
    return values


# --- Writing ---------------------------------------------------------------


def write_block(worksheet, headers, values):
    """Replace the data area below the header with `values`."""
    existing_rows = len(worksheet.get_all_values())
    needed = len(values) + 1
    if worksheet.row_count < needed:
        worksheet.add_rows(needed - worksheet.row_count + 50)

    end_col = chr(ord("A") + len(headers) - 1)
    if values:
        worksheet.update(
            values=values,
            range_name=f"A2:{end_col}{len(values) + 1}",
            value_input_option="USER_ENTERED",
        )

    # Clear any rows left over from a shorter run.
    if existing_rows > needed:
        worksheet.batch_clear([f"A{needed + 1}:{end_col}{existing_rows}"])


def append_removed(worksheet, dropped, as_of=None):
    """Tombstone removed listings so the next run doesn't re-add them."""
    if not dropped:
        return
    as_of = as_of or today()
    worksheet.append_rows(
        [[d.company, d.role, d.apply_url, as_of.isoformat()] for d in dropped],
        value_input_option="USER_ENTERED",
    )
    log.info("Tombstoned %d removed listing(s)", len(dropped))
