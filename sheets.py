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
from gspread.http_client import BackOffHTTPClient
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
    # Google hands out transient 503s; retry them instead of losing the whole run.
    client = gspread.authorize(creds, http_client=BackOffHTTPClient)
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

    existing_values = worksheet.get_all_values()
    old_headers = existing_values[0] if existing_values else []
    if old_headers and old_headers != headers and not read_only:
        _migrate_columns(worksheet, old_headers, headers, existing_values[1:])
    elif not old_headers and not read_only:
        worksheet.update(values=[headers], range_name="A1")
    return worksheet


def _migrate_columns(worksheet, old_headers, new_headers, data_rows):
    """Rewrite existing rows into a changed column layout, matching by name.

    Without this, adding or reordering a column silently shifts every existing
    row: the header would be rewritten first, and the next read would map new
    column positions onto old data. Values whose column is gone are dropped;
    new columns start empty.
    """
    log.info(
        "Migrating %r from %d to %d columns", worksheet.title, len(old_headers), len(new_headers)
    )
    position = {name: i for i, name in enumerate(old_headers)}

    migrated = []
    for row in data_rows:
        if not any(cell.strip() for cell in row):
            continue
        migrated.append(
            [
                row[position[name]] if name in position and position[name] < len(row) else ""
                for name in new_headers
            ]
        )

    end_col = _column_letter(len(new_headers))
    worksheet.update(values=[new_headers], range_name="A1")
    if migrated:
        worksheet.update(
            values=migrated,
            range_name=f"A2:{end_col}{len(migrated) + 1}",
            value_input_option="USER_ENTERED",
        )
    # Drop any columns that used to exist beyond the new width.
    if len(old_headers) > len(new_headers):
        stale = _column_letter(len(old_headers))
        worksheet.batch_clear([f"{_column_letter(len(new_headers) + 1)}1:{stale}{len(data_rows) + 1}"])


def _column_letter(count):
    """1 -> 'A', 26 -> 'Z', 27 -> 'AA'."""
    letters = ""
    while count > 0:
        count, remainder = divmod(count - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


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
            "deadline": cell("Deadline"),
            "link_status": cell("Link Status"),
            "last_checked": cell("Last Checked"),
            "source": cell("Source"),
            "salary": cell("Salary"),
            "applied": cell("Applied?") in TRUTHY,
            "applied_date": cell("Applied Date"),
            "remove": cell("Remove?") in TRUTHY,
        }
    return state


def needs_follow_up(row, as_of=None):
    """True for an application submitted long enough ago to be worth chasing.

    Rows whose link has since gone DEAD or CLOSED are excluded — those aren't
    waiting on a reply, they're over.
    """
    if not row.get("Applied?"):
        return False
    if row.get("Link Status") in ("DEAD", "CLOSED"):
        return False
    applied = _parse_date(row.get("Applied Date"))
    if applied is None:
        return False
    as_of = as_of or today()
    return (as_of - applied).days >= config.FOLLOW_UP_AFTER_DAYS


def needs_link_check(existing, as_of=None):
    """True if this listing has never been checked, or was checked too long ago."""
    if not existing:
        return True
    last = _parse_date(existing.get("last_checked"))
    if last is None:
        return True
    as_of = as_of or today()
    return (as_of - last).days >= config.LINK_CHECK_INTERVAL_DAYS


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


def build_rows(listings, state, removed_keys, as_of=None, link_status=None):
    """Merge parsed listings with sheet state. Returns (rows, newly_added, dropped).

    Precedence: the user's Remove? wins over everything. Their Applied? value is
    carried forward untouched. Date Added is preserved from the existing row so
    a listing's NEW window doesn't restart on every run.
    """
    as_of = as_of or today()
    today_str = as_of.isoformat()
    link_status = link_status or {}

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
            # Stamp the date the first time a row is seen as applied. The stamp
            # is never cleared afterwards: unticking the box by accident should
            # not silently destroy the record of when it was submitted.
            applied_date = existing.get("applied_date", "")
            if applied and not applied_date:
                applied_date = today_str
        else:
            date_added = today_str
            applied = False
            applied_date = ""
            newly_added.append(listing)

        # A fresh check wins; otherwise carry last week's verdict forward.
        checked = link_status.get(listing.apply_url)
        if checked:
            status, _detail, found_deadline = checked
            last_checked = today_str
        else:
            status = (existing or {}).get("link_status", "")
            last_checked = (existing or {}).get("last_checked", "")
            found_deadline = ""

        # Deadline is user-owned: only fill it when the cell is still empty.
        deadline = (existing or {}).get("deadline", "") or found_deadline

        rows.append(
            {
                "listing": listing,
                "Company": listing.company,
                "Role": listing.role,
                "Location": listing.location,
                "Apply Link": listing.apply_url,
                "Salary": listing.salary,
                "Deadline": deadline,
                "Date Added": date_added,
                "Remote?": "YES" if listing.is_remote else "NO",
                "Game?": "YES" if listing.is_game else "NO",
                "Status": status_for(date_added, as_of),
                "Link Status": status,
                "Last Checked": last_checked,
                "Source": listing.source,
                "Applied?": applied,
                "Applied Date": applied_date,
                "Remove?": False,
            }
        )

    rows.sort(key=_sort_key)
    return rows, newly_added, dropped


def _sort_key(row):
    """Applicable first, then NEW, then game, then remote, then newest, company.

    Dead and closed listings sink to the bottom rather than being deleted — the
    detection is good but not perfect, so they stay visible and reversible.
    Fresh listings surface for three days regardless of location; after that the
    sheet settles into remote-at-top. Tuples sort ascending, so each component is
    expressed as "0 means first".
    """
    unapplicable = 1 if row.get("Link Status") in ("DEAD", "CLOSED") else 0
    # A deadline in the past also sinks the row, including one typed by hand
    # that the link checker never saw.
    due = _parse_date(row.get("Deadline"))
    if due and due < (row.get("_as_of") or today()):
        unapplicable = 1
    is_new = 0 if row["Status"] == "NEW" else 1
    is_game = 0 if row.get("Game?") == "YES" else 1
    is_remote = 0 if row["Remote?"] == "YES" else 1
    added = _parse_date(row["Date Added"])
    recency = -added.toordinal() if added else 0
    return (unapplicable, is_new, is_game, is_remote, recency, row["Company"].lower())


def rows_to_values(rows):
    """Flatten merged rows into the sheet's column order."""
    return [
        [
            row["Company"],
            row["Role"],
            row["Location"],
            row["Apply Link"],
            row["Salary"],
            row["Deadline"],
            row["Date Added"],
            row["Remote?"],
            row["Game?"],
            row["Status"],
            row["Link Status"],
            row["Last Checked"],
            row["Source"],
            bool(row["Applied?"]),
            row["Applied Date"],
            bool(row["Remove?"]),
        ]
        for row in rows
    ]


def read_program_state(worksheet):
    """Return {key: {...}} for the programs tab, keyed on org + opportunity."""
    if worksheet is None:
        return {}
    rows = worksheet.get_all_values()
    if len(rows) < 2:
        return {}

    headers = rows[0]
    idx = {name: (headers.index(name) if name in headers else None) for name in config.PROGRAMS_HEADERS}
    state = {}

    for row in rows[1:]:
        def cell(name):
            i = idx.get(name)
            return row[i].strip() if i is not None and i < len(row) else ""

        org, opportunity = cell("Organization"), cell("Opportunity")
        if not (org or opportunity):
            continue
        state[(org.lower(), opportunity.lower())] = {
            "date_added": cell("Date Added"),
            "applied": cell("Applied?") in TRUTHY,
            "applied_date": cell("Applied Date"),
            "remove": cell("Remove?") in TRUTHY,
        }
    return state


def read_removed_program_keys(worksheet):
    """Program keys the user has removed. These must never be re-added."""
    if worksheet is None:
        return set()
    rows = worksheet.get_all_values()
    if len(rows) < 2:
        return set()
    removed = set()
    for row in rows[1:]:
        padded = row + [""] * 2
        org, opportunity = padded[0].strip(), padded[1].strip()
        if org or opportunity:
            removed.add((org.lower(), opportunity.lower()))
    return removed


def build_program_rows(programs, state, removed_keys, as_of=None):
    """Merge parsed programs with sheet state. Returns (values, dropped).

    Mirrors build_rows: Applied? is carried forward untouched, Remove? drops the
    row, and Date Added is preserved so it reflects first sighting.
    """
    as_of = as_of or today()
    today_str = as_of.isoformat()
    values, dropped = [], []

    for program in programs:
        key = program.key
        if key in removed_keys:
            continue

        existing = state.get(key)
        if existing and existing["remove"]:
            dropped.append(program)
            continue

        values.append(
            [
                program.org,
                program.opportunity,
                program.link,
                program.type,
                program.deadline,
                (existing or {}).get("date_added") or today_str,
                bool((existing or {}).get("applied", False)),
                False,
            ]
        )
    return values, dropped


def append_removed_programs(worksheet, dropped, as_of=None):
    """Tombstone removed programs so the next run doesn't re-add them."""
    if not dropped:
        return
    as_of = as_of or today()
    worksheet.append_rows(
        [[d.org, d.opportunity, as_of.isoformat()] for d in dropped],
        value_input_option="USER_ENTERED",
    )
    log.info("Tombstoned %d removed program(s)", len(dropped))


# --- Writing ---------------------------------------------------------------


def write_block(worksheet, headers, values):
    """Replace the data area below the header with `values`."""
    existing_rows = len(worksheet.get_all_values())
    needed = len(values) + 1
    if worksheet.row_count < needed:
        worksheet.add_rows(needed - worksheet.row_count + 50)

    end_col = _column_letter(len(headers))
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
