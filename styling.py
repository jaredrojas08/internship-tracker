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
COL_REMOTE = 5
COL_STATUS = 6
COL_APPLIED = 7
COL_REMOVE = 8


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
    return [
        text_eq(status_range, "NEW", PALETTE["dusty_rose"], bold=True),
        text_eq(status_range, "SEEN", PALETTE["muted_taupe"]),
        text_eq(remote_range, "YES", PALETTE["dusty_rose"], bold=True),
        formula([applied_range], "=$H2=TRUE", PALETTE["warm_brown"], PALETTE["white"], True),
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
                    "endIndex": 9,
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


def _table_request(sheet_id, name, headers, num_rows, header_bg, checkbox_cols=()):
    """Build a native Sheets Table over the header + data range.

    The table supplies its own header and alternating-band colors, so the
    Strawberry Kiss palette is applied through rowsProperties rather than
    through conditional formatting. Columns listed in checkbox_cols become
    real BOOLEAN columns, which is what renders them as toggles.
    """
    columns = []
    for index, title in enumerate(headers):
        column = {
            "columnIndex": index,
            "columnName": title,
            "columnType": "BOOLEAN" if index in checkbox_cols else "TEXT",
        }
        columns.append(column)

    return {
        "addTable": {
            "table": {
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
        }
    }


def _delete_table_requests(spreadsheet, sheet_ids):
    """Drop existing tables so the range can be rebuilt at the new row count.

    A table's range does not grow automatically when rows are appended below
    it, so each run tears down and recreates rather than trying to resize.
    """
    metadata = spreadsheet.fetch_sheet_metadata()
    requests = []
    for sheet in metadata.get("sheets", []):
        if sheet["properties"]["sheetId"] not in sheet_ids:
            continue
        for table in sheet.get("tables", []):
            requests.append({"deleteTable": {"tableId": table["tableId"]}})
    return requests


def _clear_conditional_rules(spreadsheet, sheet_ids):
    """Delete every existing rule on the given sheets, highest index first."""
    metadata = spreadsheet.fetch_sheet_metadata()
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


def apply_all(spreadsheet, listings_ws, programs_ws, num_listings, num_programs):
    """Apply the full palette to both visible tabs."""
    listings_id, programs_id = listings_ws.id, programs_ws.id

    sheet_ids = {listings_id, programs_id}

    # Teardown first: stale tables and rules must go before the new ones land.
    requests = _delete_table_requests(spreadsheet, sheet_ids)
    requests += _clear_conditional_rules(spreadsheet, sheet_ids)

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
            programs_id,
            "ProgramsAndFellowships",
            config.PROGRAMS_HEADERS,
            num_programs,
            PALETTE["light_warm_grey"],
        )
    )

    requests += [
        {"addConditionalFormatRule": {"rule": rule, "index": i}}
        for i, rule in enumerate(
            _conditional_rules(listings_id, num_listings, len(config.LISTINGS_HEADERS))
        )
    ]

    requests += _width_requests(listings_id)

    if not requests:
        return
    log.info("Applying %d formatting request(s)", len(requests))
    spreadsheet.batch_update({"requests": requests})
