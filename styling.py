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

    return [
        text_eq(status_range, "NEW", PALETTE["dusty_rose"], bold=True),
        text_eq(status_range, "SEEN", PALETTE["muted_taupe"]),
        text_eq(remote_range, "YES", PALETTE["dusty_rose"], bold=True),
        formula([applied_range], "=$H2=TRUE", PALETTE["warm_brown"], PALETTE["white"], True),
        # Alternating rows, lowest priority.
        formula([data], "=ISEVEN(ROW())", PALETTE["blush"]),
        formula([data], "=ISODD(ROW())", PALETTE["soft_tan"]),
    ]


def _checkbox_requests(sheet_id, num_rows):
    """Render the two user-owned columns as real checkboxes."""
    if num_rows < 1:
        return []
    validation = {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True}
    return [
        {
            "setDataValidation": {
                "range": _grid(sheet_id, 1, num_rows + 1, col, col + 1),
                "rule": validation,
            }
        }
        for col in (COL_APPLIED, COL_REMOVE)
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

    requests = _clear_conditional_rules(spreadsheet, {listings_id, programs_id})

    requests += _header_requests(
        listings_ws, config.LISTINGS_HEADERS, PALETTE["deep_berry"], PALETTE["deep_berry"]
    )
    requests += _header_requests(
        programs_ws, config.PROGRAMS_HEADERS, PALETTE["warm_brown"],
        PALETTE["light_warm_grey"],
    )

    requests += _border_requests(listings_id, num_listings, len(config.LISTINGS_HEADERS))
    requests += _border_requests(programs_id, num_programs, len(config.PROGRAMS_HEADERS))

    requests += [
        {"addConditionalFormatRule": {"rule": rule, "index": i}}
        for i, rule in enumerate(
            _conditional_rules(listings_id, num_listings, len(config.LISTINGS_HEADERS))
        )
    ]

    requests += _checkbox_requests(listings_id, num_listings)
    requests += _width_requests(listings_id)

    if not requests:
        return
    log.info("Applying %d formatting request(s)", len(requests))
    spreadsheet.batch_update({"requests": requests})
