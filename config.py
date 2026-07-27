"""Environment loading, filter keywords, and sheet configuration."""

import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

# --- Data source -----------------------------------------------------------

README_URL = (
    "https://raw.githubusercontent.com/sndsh404/summer-2027-internships/main/README.md"
)
REQUEST_TIMEOUT = 30

# --- Sheet configuration ---------------------------------------------------

LISTINGS_TAB = "Internship Listings"
PROGRAMS_TAB = "Programs & Fellowships"
REMOVED_TAB = "Removed"  # hidden tombstone tab, keeps removals from being re-added

LISTINGS_HEADERS = [
    "Company",
    "Role",
    "Location",
    "Apply Link",
    "Date Added",
    "Remote?",
    "Status",
    "Applied?",
    "Remove?",
]
PROGRAMS_HEADERS = ["Organization", "Opportunity", "Link", "Type", "Deadline", "Date Added"]
REMOVED_HEADERS = ["Company", "Role", "Apply Link", "Date Removed"]

# Columns the user owns. The script reads these but must never overwrite them
# with a default once a row exists.
USER_OWNED_COLUMNS = ("Applied?", "Remove?")

NEW_STATUS_DAYS = 3  # a listing shows as NEW for this many days

# --- Strawberry Kiss palette ----------------------------------------------

PALETTE = {
    "blush": "#E2B8AD",
    "warm_brown": "#87564B",
    "deep_berry": "#6D322A",
    "soft_tan": "#D2BDAB",
    "dusty_rose": "#CFA195",
    "muted_taupe": "#A59383",
    "light_warm_grey": "#C6B8AB",
    "white": "#FFFFFF",
}


def hex_to_rgb(hex_color):
    """'#E2B8AD' -> {'red': 0.886, 'green': 0.722, 'blue': 0.678} for the Sheets API."""
    h = hex_color.lstrip("#")
    return {
        "red": int(h[0:2], 16) / 255,
        "green": int(h[2:4], 16) / 255,
        "blue": int(h[4:6], 16) / 255,
    }


# --- Role filter -----------------------------------------------------------

# Matched case-insensitively against the role title. Everything here is applied
# with word boundaries so short tokens can't match inside unrelated words.
ROLE_KEYWORDS = [
    "software",
    "swe",
    "developer",
    "engineer",
    "engineering",
    "full stack",
    "fullstack",
    "backend",
    "frontend",
    "front end",
    "back end",
    "game",
    "gaming",
    "graphics",
    "unity",
    "unreal",
    "gameplay",
    "interactive",
    "simulation",
    "systems",
    "platform",
    "infrastructure",
    "mobile",
    "ios",
    "android",
    "web",
    "react",
    "application",
    "applications",
    "apps",
    "cim",
    "digital technology",
    "ai",
    "genai",
    "ml",
    "machine learning",
    "data scientist",
    "data science",
    "data analyst",
    "quant",
    "quantitative",
]

# Two-letter tokens are the dangerous ones: a bare substring search for "ai"
# matches Retail, Maintenance, Chair; "ml" matches HTML. Word boundaries on both
# sides fix that. "genai" is listed explicitly since \bai\b won't catch it.
_ROLE_PATTERN = re.compile(
    "|".join(rf"\b{re.escape(kw)}\b" for kw in ROLE_KEYWORDS),
    re.IGNORECASE,
)


def matches_role_filter(role_title):
    """True if the role title contains any whitelisted keyword as a whole word."""
    return bool(_ROLE_PATTERN.search(role_title))


# --- Credentials -----------------------------------------------------------


def load_credentials_info():
    """Return the service account dict.

    GOOGLE_SHEETS_CREDENTIALS holds a file path locally and the raw JSON string
    in GitHub Actions. Detect which and handle both.
    """
    raw = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    if not raw:
        raise RuntimeError(
            "GOOGLE_SHEETS_CREDENTIALS is not set. "
            "Locally: copy .env.example to .env and fill it in. "
            "In CI: check the repository secret."
        )

    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_SHEETS_CREDENTIALS looks like JSON but failed to parse. "
                "Re-paste the entire key file into the secret."
            ) from exc

    if not os.path.isfile(stripped):
        raise RuntimeError(
            f"GOOGLE_SHEETS_CREDENTIALS points at {stripped!r}, which does not exist."
        )
    with open(stripped, encoding="utf-8") as handle:
        return json.load(handle)


def get_sheet_id():
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID is not set.")
    return sheet_id
