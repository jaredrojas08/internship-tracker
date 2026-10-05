"""Environment loading and filter keywords."""

import datetime as dt
import os
import re

from dotenv import load_dotenv

load_dotenv()


def today():
    return dt.date.today()


# --- Data source -----------------------------------------------------------

README_URL = (
    "https://raw.githubusercontent.com/sndsh404/summer-2027-internships/main/README.md"
)
REQUEST_TIMEOUT = 30

# An application with no response after this long is worth chasing.
FOLLOW_UP_AFTER_DAYS = 21

# The full digest is daily, but a row marked Applying is waiting on Jared to
# act, so it gets chased on its own faster clock.
NAG_INTERVAL_HOURS = 2

# Send a short heartbeat on days with nothing to report, so silence always
# means "the run failed" rather than "nothing happened". Set the env var to
# "false"/"0" to only hear from the digest when something actually changed.
NOTIFY_ON_QUIET_DAYS = os.environ.get("NOTIFY_ON_QUIET_DAYS", "true").strip().lower() not in (
    "false",
    "0",
    "no",
)


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
    # Found dropped by the live sources in Oct 2026: real software work with
    # none of the words above in the title.
    "programmer",
    "programming",
    "devops",
    "site reliability",
    "sre",
    "cloud",
    "cybersecurity",
    "firmware",
    "embedded",
    "sdet",
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
# a graduate degree are unreachable and should never reach the database.
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


# --- Term extraction ---------------------------------------------------------

# The README keeps "Western Digital Summer 2027" and "Winter 2027 Co-op" as
# separate rows on purpose, so the term named in a role title is real signal,
# not decoration.
_SEASON_WORDS = r"(?:summer|winter|fall|spring)"
_SEASON_ABBR = {"su": "Summer", "wi": "Winter", "fa": "Fall", "sp": "Spring"}

_TERM_PATTERN = re.compile(
    rf"""
    \b(?P<season1>{_SEASON_WORDS})\b\s*'?\s*(?P<year1>20\d{{2}}|\d{{2}})\b
    |
    \b(?P<year2>20\d{{2}})\b\s*\b(?P<season2>{_SEASON_WORDS})\b
    |
    \b(?P<abbr>su|wi|fa|sp)(?P<year3>\d{{2}})\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def term_for(role_title):
    """Extract a term like "Summer 2027" from a role title.

    Handles both orderings ("Summer 2027" / "2027 Summer"), the apostrophe-year
    shorthand ("Summer '27"), and two-letter season codes ("Su27"). Returns
    "Unspecified" when the title names no term.
    """
    match = _TERM_PATTERN.search(role_title)
    if not match:
        return "Unspecified"
    if match.group("abbr"):
        season = _SEASON_ABBR[match.group("abbr").lower()]
        year = match.group("year3")
    else:
        season = (match.group("season1") or match.group("season2")).capitalize()
        year = match.group("year1") or match.group("year2")
    if len(year) == 2:
        year = f"20{year}"
    return f"{season} {year}"


# --- Credentials -----------------------------------------------------------


def require_env(name):
    """Read an environment variable or fail with a message that says what to do."""
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set. Locally: add it to .env. "
            f"In CI: Settings -> Secrets and variables -> Actions."
        )
    return value
