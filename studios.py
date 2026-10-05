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
import html
import json
import logging
import re
import urllib.error
import urllib.request

import ats
import config
import parser as md

log = logging.getLogger(__name__)

TIMEOUT = 15
MAX_WORKERS = 8
USER_AGENT = "InternshipTracker (https://github.com/jaredrojas08/Internship-Tracker, 1.0)"

# (display name, board slug, ats). Greenhouse, Ashby and Lever take the bare
# board name. Workday takes "tenant.wdN/Site", the two halves of the careers
# URL. Avature takes the portal host.
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
    # Added 2026-09 after probing ~300 slugs. Greenhouse slugs are checked
    # against the board's own company_name; Ashby doesn't return one, so those
    # are confirmed by their posted roles (Marvel SNAP, Guild Wars, etc.).
    ("2K", "2k", "greenhouse"),
    ("Take-Two Interactive", "taketwo", "greenhouse"),
    ("Nintendo of America", "nintendo", "greenhouse"),
    ("Insomniac Games", "insomniac", "greenhouse"),
    ("Gearbox", "gearbox", "greenhouse"),
    ("Crystal Dynamics", "crystaldynamics", "greenhouse"),
    ("Azra Games", "azragames", "greenhouse"),
    ("HoYoverse", "hoyoverse", "ashby"),
    ("ArenaNet", "arenanet", "ashby"),
    ("Second Dinner", "seconddinner", "ashby"),
    ("Believer", "believer", "ashby"),
    ("Theorycraft Games", "theorycraftgames", "lever"),
    ("Zynga", "zyngacareers", "greenhouse"),
    # Activision and Blizzard share Microsoft's Workday tenant; the Activision
    # site also carries Raven, Sledgehammer, Demonware and the other studios.
    ("Blizzard Entertainment", "xboxgaming.wd1/Blizzard_External_Careers", "workday"),
    ("Activision", "xboxgaming.wd1/External", "workday"),
    ("Unity", "unitytech.wd1/Unity", "workday"),
    ("Electronic Arts", "jobs.ea.com", "avature"),
]

ENDPOINTS = {
    # content=true returns every posting's body in the same request, so a
    # studio listing arrives already enriched and never needs a repair pass.
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
}

# Word-boundary anchored so "International" can't match "intern".
INTERN_PATTERN = re.compile(
    r"\bintern\b|\binterns\b|\binternships?\b|\bco-?ops?\b|\bapprentice\w*\b"
    r"|\bnew grad\b|\buniversity grad\w*\b|\bearly career\b",
    re.IGNORECASE,
)

# Recruiting-team roles that mention early-career talent without being one.
NOT_A_ROLE = re.compile(
    r"talent acquisition|recruit(er|ing)|talent business partner|program manager,",
    re.IGNORECASE,
)

# Non-tech functions. Studio boards skip the keyword filter so art and design
# roles get through, which also lets these in. A tech keyword in the title still wins.
NON_TECH = re.compile(
    r"\b(communications?|legal|customer experience|marketing|public relations"
    r"|finance|accounting|human resources|sales|brand|social media)\b",
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
    r"|south africa|nigeria|kenya|chile|colombia|peru|serbia|czechia|brno"
    r"|barcelona|montevideo|uruguay|bengaluru|bangalore|tel aviv|seoul|frankfurt"
    r"|kuala lumpur|bucharest|budapest|munich)\b",
    re.IGNORECASE,
)


def _request(url, data=None):
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def _get(url, data=None):
    return json.loads(_request(url, data))


def _normalize_greenhouse(payload):
    for job in payload.get("jobs", []):
        yield (
            job.get("title", ""),
            (job.get("location") or {}).get("name", ""),
            job.get("absolute_url", ""),
            ats.html_to_text(html.unescape(job.get("content", "") or "")),
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

WORKDAY_PAGE = 20  # the API's maximum


def _fetch_workday(slug):
    """Page through a Workday careers site. slug is "tenant.wdN/Site"."""
    host, site = slug.split("/", 1)
    tenant = host.split(".")[0]
    base = f"https://{host}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    jobs, offset, total = [], 0, None
    while True:
        body = json.dumps(
            {"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset, "searchText": ""}
        ).encode()
        payload = _get(api, data=body)
        # Only the first page reports the total; later pages say 0.
        if total is None:
            total = payload.get("total", 0)
        page = payload.get("jobPostings", [])
        for job in page:
            jobs.append(
                (
                    job.get("title", ""),
                    job.get("locationsText", "") or "",
                    f"{base}/{site}{job.get('externalPath', '')}",
                )
            )
        offset += WORKDAY_PAGE
        if not page or offset >= total:
            return jobs


AVATURE_ARTICLE = re.compile(r"<article.*?</article>", re.S)
AVATURE_TITLE = re.compile(
    r'article__header__text__title[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>\s*(.*?)\s*</a>', re.S
)
AVATURE_LOCATION = re.compile(r"list-item-location[^>]*>(.*?)</", re.S)


def _fetch_avature(host):
    """EA's Avature portal has no JSON API, but its search page is server-rendered.

    Searches for "intern" because the portal returns 20 results a page and the
    full list runs to thousands. EA titles its internships "Intern", not co-op.
    """
    jobs, offset = [], 0
    while True:
        url = f"https://{host}/careers/SearchJobs/intern?jobOffset={offset}"
        page = _request(url).decode("utf-8", errors="replace")
        articles = AVATURE_ARTICLE.findall(page)
        for article in articles:
            title = AVATURE_TITLE.search(article)
            if not title:
                continue
            location = AVATURE_LOCATION.search(article)
            jobs.append(
                (
                    html.unescape(title.group(2)).strip(),
                    html.unescape(re.sub(r"\s+", " ", location.group(1))).strip() if location else "",
                    title.group(1),
                )
            )
        if len(articles) < 20:
            return jobs
        offset += 20


FETCHERS = {
    "workday": _fetch_workday,
    "avature": _fetch_avature,
}


def _fetch_board(entry):
    """Return (display_name, [(title, location, url), ...], error)."""
    display, slug, ats = entry
    try:
        if ats in ENDPOINTS:
            jobs = list(NORMALIZERS[ats](_get(ENDPOINTS[ats].format(slug))))
        else:
            jobs = FETCHERS[ats](slug)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return display, [], f"{type(exc).__name__}"
    except (AttributeError, TypeError, KeyError) as exc:
        return display, [], f"unexpected payload shape: {exc}"
    return display, jobs, ""


def is_relevant(title, location):
    """Intern-shaped, US-based, and not gated on a graduate degree."""
    if not INTERN_PATTERN.search(title):
        return False
    if NOT_A_ROLE.search(title):
        return False
    if not config.is_eligible(title):
        return False
    if NON_TECH.search(title) and not config.matches_role_filter(title):
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
            for title, location, url, *rest in jobs:
                description = rest[0] if rest else ""
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
                        description=description,
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
