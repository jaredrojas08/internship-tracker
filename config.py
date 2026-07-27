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
    "Link Status",
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
    # Research/science roles that are ML work under another name.
    "applied scientist",
    "research scientist",
    "student researcher",
    "informatics",
    # Product roles adjacent to engineering.
    "product manager",
    "product management",
    "product development",
    "associate product",
    # Tech-org roles that don't say "software".
    "information technology",
]

# Uppercase acronyms matched case-sensitively. "IT" cannot go in the list above:
# case-insensitively it would match the English word "it", and its inflection
# "its", in any role title.
CASE_SENSITIVE_KEYWORDS = ["IT", "CIM", "SWE"]

# Matching rules, in tension with each other:
#   - Bare substring search is too loose: "ai" matches Retail/Maintenance/Chair,
#     "ml" matches HTML.
#   - Strict \bkw\b is too tight: it misses "Platforms" and "Engineered".
# So: word-boundary anchored, with an optional common inflection suffix. That
# catches platforms/engineered/engineering while still rejecting Retail (ai+l)
# and HTML (ht+ml). "genai" is listed explicitly since \bai\b won't reach it.
_INFLECTIONS = r"(?:s|es|ed|ing)?"
_ROLE_PATTERN = re.compile(
    "|".join(rf"\b{re.escape(kw)}{_INFLECTIONS}\b" for kw in ROLE_KEYWORDS),
    re.IGNORECASE,
)
_ACRONYM_PATTERN = re.compile(
    "|".join(rf"\b{re.escape(kw)}\b" for kw in CASE_SENSITIVE_KEYWORDS)
)

# --- Eligibility -----------------------------------------------------------

# Jared is a BS Computer Science student graduating May 2028, so roles gated on
# a graduate degree are unreachable and should never reach the sheet.
_ADVANCED_DEGREE = re.compile(
    r"\bph\.?\s?d\b|\bmaster'?s?\b|\bmba\b|\bdoctoral\b|\bpost[- ]?doc\w*\b|\bm\.?s\.?\b",
    re.IGNORECASE,
)

# ...unless the posting also opens the door to undergrads, as in "Intern
# (BS/MS/PhD)". In that case the graduate degree is one option, not a gate.
_UNDERGRAD_OK = re.compile(
    r"\bb\.?s\.?\b|\bbachelor'?s?\b|\bundergrad\w*\b|\bsophomore\b|\bjunior\b|\bfreshman\b",
    re.IGNORECASE,
)


def requires_advanced_degree(role_title):
    """True if the role is gated on a graduate degree Jared won't have."""
    if not _ADVANCED_DEGREE.search(role_title):
        return False
    return not _UNDERGRAD_OK.search(role_title)


def matches_role_filter(role_title):
    """True if the role is both relevant and something Jared is eligible for."""
    if requires_advanced_degree(role_title):
        return False
    return bool(_ROLE_PATTERN.search(role_title)) or bool(
        _ACRONYM_PATTERN.search(role_title)
    )


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
