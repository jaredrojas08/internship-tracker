"""Strawberry Kiss formatting, applied idempotently on every run.

All formatting goes through spreadsheets.batchUpdate. Conditional format rules
are deleted and re-added each run rather than appended, so repeated runs can't
stack duplicates.
"""

import logging
from typing import Any

import config
from config import PALETTE, hex_to_rgb

log = logging.getLogger(__name__)

# 0-based column positions in the listings tab.
COL_APPLY_LINK = 3
COL_SALARY = 4
COL_DEADLINE = 5
COL_REMOTE = 7
COL_GAME = 8
COL_STATUS = 9
COL_LINK_STATUS = 10
COL_LAST_CHECKED = 11
COL_SOURCE = 12
COL_APPLIED = 13
COL_APPLIED_DATE = 14
COL_REMOVE = 15

# Programs tab.
COL_PROG_DEADLINE = 4
COL_PROG_APPLIED = 6
COL_PROG_REMOVE = 7


def _a1(index):
    """0-based column index -> A1 letter, for use inside conditional formulas."""
    return chr(ord("A") + index)


A1_LINK_STATUS = _a1(COL_LINK_STATUS)
A1_APPLIED = _a1(COL_APPLIED)
A1_DEADLINE = _a1(COL_DEADLINE)


def _grid(sheet_id, start_row=0, end_row=None, start_col=0, end_col=None):
    grid = {"sheetId": sheet_id, "startRowIndex": start_row, "startColumnIndex": start_col}
    if end_row is not None:
        grid["endRowIndex"] = end_row
    if end_col is not None:
        grid["endColumnIndex"] = end_col
    return grid


def _text_format(color=None, bold=False):
    fmt: dict[str, Any] = {"bold": bold}
    if color:
        fmt["foregroundColor"] = hex_to_rgb(color)
    return fmt


def _header_requests(worksheet, headers, tab_color, header_bg):
    sheet_id = worksheet.id
    return [
        {
            "repeatCell": {
                "range": _grid(sheet_id, 0, 1, 0, len(headers)),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": hex_to_rgb(header_bg),
                        "textFormat": _text_format(PALETTE["white"], bold=True),
                        "horizontalAlignment": "LEFT",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                    "tabColor": hex_to_rgb(tab_color),
                },
                "fields": "gridProperties.frozenRowCount,tabColor",
            }
        },
    ]


def _border_requests(sheet_id, num_rows, num_cols):
    if num_rows < 1:
        return []
    line = {"style": "SOLID", "width": 1, "color": hex_to_rgb(PALETTE["light_warm_grey"])}
    return [
        {
            "updateBorders": {
                "range": _grid(sheet_id, 0, num_rows + 1, 0, num_cols),
                "innerHorizontal": line,
                "innerVertical": line,
                "top": line,
                "bottom": line,
                "left": line,
                "right": line,
            }
        }
    ]


def _conditional_rules(sheet_id, num_rows, num_cols):
    """Highest priority first. Banding goes last so specific rules win."""
    if num_rows < 1:
        return []

    data = _grid(sheet_id, 1, num_rows + 1, 0, num_cols)
    status_range = _grid(sheet_id, 1, num_rows + 1, COL_STATUS, COL_STATUS + 1)
    remote_range = _grid(sheet_id, 1, num_rows + 1, COL_REMOTE, COL_REMOTE + 1)
    applied_range = _grid(sheet_id, 1, num_rows + 1, COL_APPLIED, COL_APPLIED + 1)
    link_range = _grid(sheet_id, 1, num_rows + 1, COL_LINK_STATUS, COL_LINK_STATUS + 1)
    whole_row = _grid(sheet_id, 1, num_rows + 1, 0, num_cols)

    def text_eq(ranges, value, bg, fg=None, bold=False):
        fmt: dict[str, Any] = {"backgroundColor": hex_to_rgb(bg)}
        if fg or bold:
            fmt["textFormat"] = _text_format(fg, bold)
        return {
            "ranges": [ranges],
            "booleanRule": {
                "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": value}]},
                "format": fmt,
            },
        }

    def formula(ranges, expression, bg, fg=None, bold=False):
        fmt: dict[str, Any] = {"backgroundColor": hex_to_rgb(bg)}
        if fg or bold:
            fmt["textFormat"] = _text_format(fg, bold)
        return {
            "ranges": ranges,
            "booleanRule": {
                "condition": {
                    "type": "CUSTOM_FORMULA",
                    "values": [{"userEnteredValue": expression}],
                },
                "format": fmt,
            },
        }

    del data  # banding is handled natively by the table's rowsProperties
    deadline_range = _grid(sheet_id, 1, num_rows + 1, COL_DEADLINE, COL_DEADLINE + 1)
    game_range = _grid(sheet_id, 1, num_rows + 1, COL_GAME, COL_GAME + 1)
    ls, ap, dl = A1_LINK_STATUS, A1_APPLIED, A1_DEADLINE
    ad = _a1(COL_APPLIED_DATE)

    return [
        # Dead, closed, or past-deadline rows grey out entirely so they read as
        # inactive at a glance.
        formula(
            [whole_row],
            f'=OR(${ls}2="DEAD",${ls}2="CLOSED",AND(ISNUMBER(${dl}2),${dl}2<TODAY()))',
            PALETTE["light_warm_grey"],
            PALETTE["warm_brown"],
        ),
        text_eq(link_range, "DEAD", PALETTE["muted_taupe"], PALETTE["white"], True),
        text_eq(link_range, "CLOSED", PALETTE["muted_taupe"], PALETTE["white"], True),
        # A deadline inside the next two weeks is the thing worth acting on.
        formula(
            [deadline_range],
            f"=AND(ISNUMBER(${dl}2),${dl}2>=TODAY(),${dl}2<=TODAY()+14)",
            PALETTE["dusty_rose"],
            bold=True,
        ),
        text_eq(status_range, "NEW", PALETTE["dusty_rose"], bold=True),
        text_eq(status_range, "SEEN", PALETTE["muted_taupe"]),
        # Game roles are the priority tier, so they get the strongest fill.
        text_eq(game_range, "YES", PALETTE["deep_berry"], PALETTE["white"], True),
        text_eq(remote_range, "YES", PALETTE["dusty_rose"], bold=True),
        formula([applied_range], f"=${ap}2=TRUE", PALETTE["warm_brown"], PALETTE["white"], True),
        # An application sitting unanswered past the follow-up window.
        formula(
            [_grid(sheet_id, 1, num_rows + 1, COL_APPLIED_DATE, COL_APPLIED_DATE + 1)],
            f"=AND(${ap}2=TRUE,ISNUMBER(${ad}2),"
            f"${ad}2<=TODAY()-{config.FOLLOW_UP_AFTER_DAYS})",
            PALETTE["dusty_rose"],
            PALETTE["deep_berry"],
            True,
        ),
    ]


def _program_rules(sheet_id, num_rows):
    """Programs tab: mark applied rows, and grey out passed deadlines.

    The Deadline column here is free text from the source ("rolling",
    "check site", "approx June 14 to 30, 2026"), so the passed-deadline rule is
    guarded on ISNUMBER — it only fires when the cell holds a real date.
    """
    if num_rows < 1:
        return []

    applied_col = _a1(COL_PROG_APPLIED)
    deadline_col = _a1(COL_PROG_DEADLINE)
    width = len(config.PROGRAMS_HEADERS)

    return [
        {
            "ranges": [_grid(sheet_id, 1, num_rows + 1, 0, width)],
            "booleanRule": {
                "condition": {
                    "type": "CUSTOM_FORMULA",
                    "values": [
                        {
                            "userEnteredValue": f"=AND(ISNUMBER(${deadline_col}2),"
                            f"${deadline_col}2<TODAY())"
                        }
                    ],
                },
                "format": {
                    "backgroundColor": hex_to_rgb(PALETTE["light_warm_grey"]),
                    "textFormat": _text_format(PALETTE["warm_brown"]),
                },
            },
        },
        {
            "ranges": [
                _grid(sheet_id, 1, num_rows + 1, COL_PROG_APPLIED, COL_PROG_APPLIED + 1)
            ],
            "booleanRule": {
                "condition": {
                    "type": "CUSTOM_FORMULA",
                    "values": [{"userEnteredValue": f"=${applied_col}2=TRUE"}],
                },
                "format": {
                    "backgroundColor": hex_to_rgb(PALETTE["warm_brown"]),
                    "textFormat": _text_format(PALETTE["white"], bold=True),
                },
            },
        },
    ]


def _width_requests(sheet_id):
    """Auto-fit everything, then cap Role and Location so they can't sprawl."""
    return [
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": len(config.LISTINGS_HEADERS),
                }
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 1,
                    "endIndex": 3,
                },
                "properties": {"pixelSize": 340},
                "fields": "pixelSize",
            }
        },
    ]


def _color_style(hex_color):
    return {"rgbColor": hex_to_rgb(hex_color)}


def _table_body(sheet_id, name, headers, num_rows, header_bg, checkbox_cols=()):
    """The Table payload shared by the add and update paths.

    The table supplies its own header and alternating-band colors, so the
    Strawberry Kiss palette is applied through rowsProperties rather than
    through conditional formatting. Columns listed in checkbox_cols become
    real BOOLEAN columns, which is what renders them as toggles.
    """
    columns = [
        {
            "columnIndex": index,
            "columnName": title,
            "columnType": "BOOLEAN" if index in checkbox_cols else "TEXT",
        }
        for index, title in enumerate(headers)
    ]
    return {
        "name": name,
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 0,
            "endRowIndex": max(num_rows + 1, 2),
            "startColumnIndex": 0,
            "endColumnIndex": len(headers),
        },
        "rowsProperties": {
            "headerColorStyle": _color_style(header_bg),
            "firstBandColorStyle": _color_style(PALETTE["blush"]),
            "secondBandColorStyle": _color_style(PALETTE["soft_tan"]),
        },
        "columnProperties": columns,
    }


def _existing_table_id(metadata, sheet_id):
    for sheet in metadata.get("sheets", []):
        if sheet["properties"]["sheetId"] != sheet_id:
            continue
        tables = sheet.get("tables", [])
        if tables:
            return tables[0]["tableId"]
    return None


def _table_request(metadata, sheet_id, name, headers, num_rows, header_bg, checkbox_cols=()):
    """Create the table, or resize the existing one in place.

    Critically this never issues deleteTable: that request removes the table's
    data rows along with the table, which silently empties the sheet. Resizing
    via updateTable is the only safe way to track a changing row count.
    """
    body = _table_body(sheet_id, name, headers, num_rows, header_bg, checkbox_cols)
    table_id = _existing_table_id(metadata, sheet_id)
    if table_id is None:
        return {"addTable": {"table": body}}
    body["tableId"] = table_id
    return {
        "updateTable": {
            "table": body,
            "fields": "range,rowsProperties,columnProperties",
        }
    }


def _clear_conditional_rules(spreadsheet, sheet_ids, metadata=None):
    """Delete every existing rule on the given sheets, highest index first."""
    metadata = metadata or spreadsheet.fetch_sheet_metadata()
    requests = []
    for sheet in metadata.get("sheets", []):
        sheet_id = sheet["properties"]["sheetId"]
        if sheet_id not in sheet_ids:
            continue
        count = len(sheet.get("conditionalFormats", []))
        for index in range(count - 1, -1, -1):
            requests.append(
                {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": index}}
            )
    return requests


def sync_table_schema(spreadsheet, listings_ws, programs_ws, num_listings, num_programs):
    """Align each table's column types with the current headers, before writing.

    Must run BEFORE row data is written. A Table column declared BOOLEAN coerces
    anything written into it to TRUE/FALSE, so if the checkbox columns have moved
    (a column was inserted) the stale schema silently turns the two columns now
    sitting at the old checkbox positions into FALSE.
    """
    metadata = spreadsheet.fetch_sheet_metadata()
    requests = [
        _table_request(
            metadata,
            listings_ws.id,
            "InternshipListings",
            config.LISTINGS_HEADERS,
            num_listings,
            PALETTE["deep_berry"],
            checkbox_cols={COL_APPLIED, COL_REMOVE},
        ),
        _table_request(
            metadata,
            programs_ws.id,
            "ProgramsAndFellowships",
            config.PROGRAMS_HEADERS,
            num_programs,
            PALETTE["light_warm_grey"],
            checkbox_cols={COL_PROG_APPLIED, COL_PROG_REMOVE},
        ),
    ]
    spreadsheet.batch_update({"requests": requests})


def apply_all(spreadsheet, listings_ws, programs_ws, num_listings, num_programs):
    """Apply the full palette to both visible tabs."""
    listings_id, programs_id = listings_ws.id, programs_ws.id

    sheet_ids = {listings_id, programs_id}
    metadata = spreadsheet.fetch_sheet_metadata()

    # Conditional rules are cheap to recreate, so they are cleared and re-added.
    # Tables are not: see _table_request.
    requests = _clear_conditional_rules(spreadsheet, sheet_ids, metadata)

    # Freeze + tab colors. The tables own the header fill from here.
    requests += _header_requests(
        listings_ws, config.LISTINGS_HEADERS, PALETTE["deep_berry"], PALETTE["deep_berry"]
    )
    requests += _header_requests(
        programs_ws, config.PROGRAMS_HEADERS, PALETTE["warm_brown"],
        PALETTE["light_warm_grey"],
    )

    requests += _border_requests(listings_id, num_listings, len(config.LISTINGS_HEADERS))
    requests += _border_requests(programs_id, num_programs, len(config.PROGRAMS_HEADERS))

    requests.append(
        _table_request(
            metadata,
            listings_id,
            "InternshipListings",
            config.LISTINGS_HEADERS,
            num_listings,
            PALETTE["deep_berry"],
            checkbox_cols={COL_APPLIED, COL_REMOVE},
        )
    )
    requests.append(
        _table_request(
            metadata,
            programs_id,
            "ProgramsAndFellowships",
            config.PROGRAMS_HEADERS,
            num_programs,
            PALETTE["light_warm_grey"],
            checkbox_cols={COL_PROG_APPLIED, COL_PROG_REMOVE},
        )
    )

    requests += [
        {"addConditionalFormatRule": {"rule": rule, "index": i}}
        for i, rule in enumerate(
            _conditional_rules(listings_id, num_listings, len(config.LISTINGS_HEADERS))
        )
    ]

    requests += [
        {"addConditionalFormatRule": {"rule": rule, "index": i}}
        for i, rule in enumerate(_program_rules(programs_id, num_programs))
    ]

    requests += _width_requests(listings_id)

    if not requests:
        return
    log.info("Applying %d formatting request(s)", len(requests))
    spreadsheet.batch_update({"requests": requests})
