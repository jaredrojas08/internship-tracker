"""Game studio job boards, queried directly through their ATS APIs.

The GitHub internship lists don't carry game roles — measured across ~4,100
rows, zero. Studios post their own openings on Greenhouse, Lever and Ashby,
all of which expose public unauthenticated JSON. That's a cleaner source than
markdown scraping: structured records, and a real job id for deduplication.

Expect this to return almost nothing between February and August. Studios
recruit for summer internships roughly September through January, far later
than the quant and big-tech lists. An empty result in July is the calendar,
not a failure.

Board slugs are hand-verified. A studio that 404s here is on a different ATS,
not necessarily gone.
"""

import concurrent.futures
import json
import logging
import re
import urllib.error
import urllib.request

import config
import parser as md

log = logging.getLogger(__name__)

TIMEOUT = 15
MAX_WORKERS = 8
USER_AGENT = "InternshipTracker (https://github.com/jaredrojas08/Internship-Tracker, 1.0)"

# (display name, board slug, ats)
STUDIO_BOARDS = [
    ("Riot Games", "riotgames", "greenhouse"),
    ("Epic Games", "epicgames", "greenhouse"),
    ("Roblox", "roblox", "greenhouse"),
    ("Sony Interactive Entertainment", "sonyinteractiveentertainmentglobal", "greenhouse"),
    ("Scopely", "scopely", "greenhouse"),
    ("Rockstar Games", "rockstargames", "greenhouse"),
    ("Discord", "discord", "greenhouse"),
    ("Naughty Dog", "naughtydog", "greenhouse"),
    ("Digital Extremes", "digitalextremes", "greenhouse"),
    ("Bungie", "bungie", "greenhouse"),
    ("Supercell", "supercell", "ashby"),
    ("thatgamecompany", "thatgamecompany", "ashby"),
    ("Skydance", "skydance", "lever"),
    ("Jam City", "jamcity", "lever"),
]

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
}

# Word-boundary anchored so "International" can't match "intern".
INTERN_PATTERN = re.compile(
    r"\bintern\b|\binterns\b|\binternship\b|\bco-?op\b|\bapprentice\w*\b"
    r"|\bnew grad\b|\buniversity grad\w*\b|\bearly career\b",
    re.IGNORECASE,
)

# Recruiting-team roles that mention early-career talent without being one.
NOT_A_ROLE = re.compile(
    r"talent acquisition|recruit(er|ing)|talent business partner|program manager,",
    re.IGNORECASE,
)

# Locations to exclude. The tracker is US-focused, and studio boards are
# global — Bangalore, Shanghai and Singapore postings dominate the intern
# titles outside the US hiring season.
NON_US = re.compile(
    r"\b(india|china|shanghai|beijing|singapore|japan|tokyo|vietnam|korea|taiwan"
    r"|canada|montreal|toronto|vancouver|quebec|ontario|brazil|mexico|argentina"
    r"|united kingdom|england|london|ireland|dublin|germany|berlin|france|paris"
    r"|spain|madrid|netherlands|amsterdam|sweden|stockholm|finland|helsinki"
    r"|poland|warsaw|australia|sydney|melbourne|new zealand|philippines|thailand"
    r"|indonesia|malaysia|israel|turkey|uae|dubai|portugal|lisbon|denmark"
    r"|norway|switzerland|austria|belgium|czech|romania|hungary|greece|egypt"
    r"|south africa|nigeria|kenya|chile|colombia|peru)\b",
    re.IGNORECASE,
)


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def _normalize_greenhouse(payload):
    for job in payload.get("jobs", []):
        yield (
            job.get("title", ""),
            (job.get("location") or {}).get("name", ""),
            job.get("absolute_url", ""),
        )


def _normalize_ashby(payload):
    for job in payload.get("jobs", []):
        yield (
            job.get("title", ""),
            job.get("location", "") or "",
            job.get("jobUrl") or job.get("applyUrl") or "",
        )


def _normalize_lever(payload):
    for job in payload:
        categories = job.get("categories") or {}
        yield (
            job.get("text", ""),
            categories.get("location", "") or "",
            job.get("hostedUrl") or job.get("applyUrl") or "",
        )


NORMALIZERS = {
    "greenhouse": _normalize_greenhouse,
    "ashby": _normalize_ashby,
    "lever": _normalize_lever,
}


def _fetch_board(entry):
    """Return (display_name, [(title, location, url), ...], error)."""
    display, slug, ats = entry
    try:
        payload = _get(ENDPOINTS[ats].format(slug))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return display, [], f"{type(exc).__name__}"
    try:
        return display, list(NORMALIZERS[ats](payload)), ""
    except (AttributeError, TypeError) as exc:
        return display, [], f"unexpected payload shape: {exc}"


def is_relevant(title, location):
    """Intern-shaped, US-based, and not gated on a graduate degree."""
    if not INTERN_PATTERN.search(title):
        return False
    if NOT_A_ROLE.search(title):
        return False
    if config.requires_advanced_degree(title):
        return False
    if NON_US.search(location or ""):
        return False
    return True


def fetch_studio_listings():
    """Query every studio board. Individual board failures are tolerated."""
    listings, failures, scanned = [], [], 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for display, jobs, error in pool.map(_fetch_board, STUDIO_BOARDS):
            if error:
                failures.append(f"{display} ({error})")
                continue
            scanned += len(jobs)
            for title, location, url in jobs:
                if not url or not is_relevant(title, location):
                    continue
                listings.append(
                    md.Listing(
                        company=display,
                        role=title.strip(),
                        location=location.strip(),
                        apply_url=url.strip(),
                        source="studios",
                        # Anything from a studio board counts as game work, even
                        # when the title has no game keyword in it.
                        from_game_studio=True,
                    )
                )

    if failures:
        log.warning("Studio boards unreachable: %s", "; ".join(failures))

    # An empty result is normal out of season, so it can't be treated as an
    # error. That makes a total outage invisible unless it's raised explicitly:
    # every board failing is infrastructure, not the calendar.
    if len(failures) == len(STUDIO_BOARDS):
        raise RuntimeError(
            f"all {len(STUDIO_BOARDS)} studio boards unreachable: {'; '.join(failures[:3])}"
        )
    if len(failures) > len(STUDIO_BOARDS) // 2:
        raise RuntimeError(
            f"{len(failures)} of {len(STUDIO_BOARDS)} studio boards unreachable: "
            f"{'; '.join(failures[:3])}"
        )

    log.info(
        "studios: %d relevant of %d job(s) across %d board(s)",
        len(listings),
        scanned,
        len(STUDIO_BOARDS) - len(failures),
    )
    return listings
