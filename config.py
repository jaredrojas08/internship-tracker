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
REMOVED_PROGRAMS_TAB = "Removed Programs"  # same idea, but programs key on 2 fields

LISTINGS_HEADERS = [
    "Company",
    "Role",
    "Location",
    "Apply Link",
    "Salary",
    "Deadline",
    "Date Added",
    "Remote?",
    "Game?",
    "Link Status",
    "Last Checked",
    "Source",
    "Application",
    "Applied Date",
    "Remove?",
]
# The Application dropdown, in order. The first is the default for a new row.
APPLICATION_STATES = ("Not Applied", "Applying", "Applied")
PROGRAMS_HEADERS = [
    "Organization",
    "Opportunity",
    "Link",
    "Type",
    "Deadline",
    "Date Added",
    "Applied?",
    "Remove?",
]
REMOVED_HEADERS = ["Company", "Role", "Apply Link", "Date Removed"]
REMOVED_PROGRAMS_HEADERS = ["Organization", "Opportunity", "Date Removed"]

# Columns the user owns. The script reads these but must never overwrite them
# with a default once a row exists. Deadline is here because only ~3% of job
# postings state one in machine-readable form, so it is mostly typed by hand;
# the script fills it only when the row is still blank.
USER_OWNED_COLUMNS = ("Applied?", "Remove?", "Deadline")

# An application with no response after this long is worth chasing.
FOLLOW_UP_AFTER_DAYS = 21

# Send a short heartbeat on days with nothing to report, so silence always
# means "the run failed" rather than "nothing happened". Set the env var to
# "false"/"0" to only hear from the digest when something actually changed.
NOTIFY_ON_QUIET_DAYS = os.environ.get("NOTIFY_ON_QUIET_DAYS", "true").strip().lower() not in (
    "false",
    "0",
    "no",
)


# Application links are re-checked on this cadence rather than every run.
# A listing the script has never checked is always checked immediately.
LINK_CHECK_INTERVAL_DAYS = 7

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
    "red": "#B03A2E",
    "amber": "#D4A054",
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

# --- Game development priority ---------------------------------------------

# Jared's specialty: Unity/C# game dev, a game design minor, and a game studio
# internship. Roles matching these sort above everything except brand-new ones.
#
# "engine" is deliberately absent: it matches jet engines, search engines and
# rules engines far more often than game engines. "game engine" is listed in
# full instead.
GAME_KEYWORDS = [
    "game",
    "gaming",
    "gameplay",
    "game engine",
    "game design",
    "level design",
    "unity",
    "unreal",
    "godot",
    "graphics",
    "rendering",
    "shader",
    "animation",
    "technical artist",
    "virtual reality",
    "augmented reality",
    "mixed reality",
    "vr",
    "xr",
    "3d",
]

_GAME_PATTERN = re.compile(
    "|".join(rf"\b{re.escape(kw)}{'(?:s|es|ed|ing)?'}\b" for kw in GAME_KEYWORDS),
    re.IGNORECASE,
)


def is_game_role(role_title):
    """True if the role looks like game development work."""
    return bool(_GAME_PATTERN.search(role_title))


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


# Full-time offers for a company's own returning interns. Not open to anyone else.
_INTERN_CONVERSION = re.compile(r"\bintern conversions?\b", re.IGNORECASE)

# Design tracks Jared isn't on. Level and game design stay: those are the minor.
# The qualifier must be followed by "design", so "Software Engineer: UI/UX" and
# "UX/UI Front End Engineer" are still engineering roles.
_DESIGN_TRACK = re.compile(
    r"\b(product|ux|ui/ux|ux/ui|ui|user experience|experience|visual|graphic|interaction)"
    r" design(er|ers)?\b",
    re.IGNORECASE,
)


def requires_advanced_degree(role_title):
    """True if the role is gated on a graduate degree Jared won't have."""
    if not _ADVANCED_DEGREE.search(role_title):
        return False
    return not _UNDERGRAD_OK.search(role_title)


def is_eligible(role_title):
    """False for roles that aren't for Jared however relevant the keywords look."""
    if requires_advanced_degree(role_title):
        return False
    if _INTERN_CONVERSION.search(role_title):
        return False
    return not _DESIGN_TRACK.search(role_title)


def matches_role_filter(role_title):
    """True if the role is both relevant and something Jared is eligible for."""
    if not is_eligible(role_title):
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
