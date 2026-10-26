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
import xml.etree.ElementTree as ET

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
    # SIE posts its US internships on a separate board; the global one carries none.
    ("Sony Interactive Entertainment", "usinternshipspubliccareerssite", "greenhouse"),
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
    # Slot machine and social casino studios, both with Las Vegas offices.
    ("Light & Wonder", "lnw.wd5/LightWonderExternalCareers", "workday"),
    ("Aristocrat", "aristocrat.wd3/AristocratExternalCareersSite", "workday"),
    ("Electronic Arts", "jobs.ea.com", "avature"),
    # Added 2026-10-26 from a probe of ~2,400 game companies (Wikipedia lists plus
    # sorcery.gg's studio directory). Boards with no US jobs today stay in when the
    # studio is US-based, so its internships show up once posted.
    ("Hasbro", "hasbro", "greenhouse"),
    ("AppLovin", "applovin", "greenhouse"),
    ("Absurd Ventures", "absurdventures", "greenhouse"),
    ("teamLFG", "teamlfg", "greenhouse"),
    ("31st Union", "31stunion", "greenhouse"),
    ("Atari", "atariinc", "greenhouse"),
    ("Mob Entertainment", "mobentertainment", "greenhouse"),
    ("NC America", "ncamerica", "greenhouse"),
    ("MobilityWare", "mobilityware", "greenhouse"),
    ("Skillz", "skillzinc", "greenhouse"),
    ("Rushdown Studios", "rushdownstudios", "greenhouse"),
    ("Visual Concepts", "visualconcepts", "greenhouse"),
    ("Ghost Story Games", "gsgcareers", "greenhouse"),
    ("Linden Lab", "lindenlab", "greenhouse"),
    ("NetEase Games", "neteasegames", "greenhouse"),
    ("Panic Button", "panicbutton", "greenhouse"),
    ("Daybreak Games", "daybreakgames", "greenhouse"),
    ("KRAFTON Americas", "kraftonamericas", "greenhouse"),
    ("That's No Moon", "thatsnomoonentertainment", "greenhouse"),
    ("Tripledot Studios", "tripledotstudios", "greenhouse"),
    ("Bonfire Studios", "bonfirestudiosinc", "greenhouse"),
    ("Firaxis Games", "firaxis", "greenhouse"),
    ("Tripwire Interactive", "tripwireinteractive", "greenhouse"),
    ("Wargaming", "wargamingen", "greenhouse"),
    ("2K", "2kearlycareers", "greenhouse"),
    ("Hangar 13", "hangar13", "greenhouse"),
    ("Turtle Rock Studios", "turtlerockstudios", "greenhouse"),
    ("Psyonix", "psyonix", "greenhouse"),
    ("Pocket Gems", "pocketgems", "greenhouse"),
    ("Fortis Games", "fortisgames", "greenhouse"),
    ("Bit Reactor", "bitreactor", "greenhouse"),
    ("Digital Eclipse", "digitaleclipse", "greenhouse"),
    ("Unknown Worlds", "unknownworlds", "greenhouse"),
    ("Chartboost", "chartboost", "greenhouse"),
    ("Eleventh Hour Games", "eleventhhourgames", "greenhouse"),
    ("Niantic Spatial", "niantic-spatial", "ashby"),
    ("Spellbrush", "spellbrush", "ashby"),
    ("Voodoo", "voodoo", "ashby"),
    ("Bullet Farm", "bulletfarm", "ashby"),
    ("Balor Games", "balorgames", "ashby"),
    ("Joyteractive", "joyteractive", "ashby"),
    ("TapBlaze", "tapblaze", "ashby"),
    ("Gardens", "gardens", "ashby"),
    ("Sky Mavis", "skymavis", "ashby"),
    ("Demiurge Studios", "demiurgestudios", "lever"),
    ("Immutable", "immutable", "lever"),
    ("Kabam", "kabam", "lever"),
    ("Scientific Games", "sglottery.wd5/ScientificGamesExternalCareers", "workday"),
    ("Tencent", "tencent.wd1/Tencent_Careers", "workday"),
    ("TiMi Studio Group", "tencent.wd1/timi_careers", "workday"),
    ("Sledgehammer Games", "xboxgaming.wd1/CentralTech", "workday"),
    ("CD Projekt Red", "cdprojektred", "smartrecruiters"),
    ("Intrepid Studios", "intrepidstudios", "smartrecruiters"),
    ("Gameloft", "gameloft", "smartrecruiters"),
    ("Crazy Maple Studio", "crazymaplestudio", "workable"),
    ("Velan Studios", "velanstudios", "workable"),
    ("Team17", "team-17-digital", "workable"),
    ("Jackbox Games", "jackbox-games", "workable"),
    ("Arkadium", "arkadium-1", "workable"),
    ("Stardock", "stardock", "bamboohr"),
    ("Dreamhaven", "dreamhaven", "bamboohr"),
    ("Ember Lab", "emberlab", "bamboohr"),
    ("inXile Entertainment", "inxile", "bamboohr"),
    ("Schell Games", "sg", "bamboohr"),
    ("High Voltage Software", "highvoltagesoftware", "bamboohr"),
    ("High 5 Games", "high5games", "bamboohr"),
    ("Studio Wildcard", "studiowildcard", "bamboohr"),
    ("Filament Games", "filamentgames", "bamboohr"),
    ("Motorsport Games", "motorsportgames", "bamboohr"),
    ("Ruckus Games", "careers.ruckus-games.com", "teamtailor"),
    ("OtherSide Entertainment", "careers.otherside-e.com", "teamtailor"),
    ("AdHoc Studio", "careers.adhocla.com", "teamtailor"),
]

ENDPOINTS = {
    # content=true returns every posting's body in the same request, so a
    # studio listing arrives already enriched and never needs a repair pass.
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{}",
}

# Word-boundary anchored so "International" can't match "intern".
INTERN_PATTERN = re.compile(
    r"\bintern\b|\binterns\b|\binternships?\b|\bco-?ops?\b|\bapprentice\w*\b"
    r"|\bnew grad\b|\buniversity grad\w*\b|\bearly career\b",
    re.IGNORECASE,
)

# Wider than INTERN_PATTERN on purpose. A title this catches and that pattern
# misses is a wording the tracker has not seen, so it goes to Notion for review.
EARLY_CAREER_HINT = re.compile(
    r"\bintern(?!al|ation)|\bco-?op|\bapprentic|early.career|new.grad|graduate|student"
    r"|university|campus|summer\s*'?20\d\d|20\d\d\s*summer|trainee|stagiaire|placement|fellowship",
    re.IGNORECASE,
)

# Recruiting-team roles that mention early-career talent without being one.
NOT_A_ROLE = re.compile(
    r"talent acquisition|talent sourcing|recruit(er|ing)|talent business partner|program manager,",
    re.IGNORECASE,
)

# Non-tech functions. Studio boards skip the keyword filter so art and design
# roles get through, which also lets these in. A tech keyword in the title still wins.
NON_TECH = re.compile(
    r"\b(communications?|legal|customer experience|marketing|public relations"
    r"|finance|accounting|human resources|sales|brand|social media|pr|consumer insights)\b",
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


def _normalize_workable(payload):
    for job in payload.get("jobs", []):
        location = ", ".join(
            part for part in (job.get("city"), job.get("state"), job.get("country")) if part
        )
        yield (job.get("title", ""), location, job.get("url") or job.get("application_url") or "")


def _normalize_bamboohr(payload, slug):
    for job in payload.get("result", []):
        place = job.get("location") or {}
        location = ", ".join(part for part in (place.get("city"), place.get("state")) if part)
        if job.get("isRemote"):
            location = f"{location} (Remote)".strip()
        yield (
            job.get("jobOpeningName", ""),
            location,
            f"https://{slug}.bamboohr.com/careers/{job.get('id')}",
        )


NORMALIZERS = {
    "greenhouse": _normalize_greenhouse,
    "ashby": _normalize_ashby,
    "lever": _normalize_lever,
    "workable": _normalize_workable,
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


SMARTRECRUITERS_PAGE = 100  # the API's maximum


def _fetch_smartrecruiters(slug):
    jobs, offset = [], 0
    while True:
        payload = _get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
            f"?limit={SMARTRECRUITERS_PAGE}&offset={offset}"
        )
        page = payload.get("content", [])
        for job in page:
            jobs.append(
                (
                    job.get("name", ""),
                    (job.get("location") or {}).get("fullLocation", "") or "",
                    f"https://jobs.smartrecruiters.com/{slug}/{job.get('id')}",
                )
            )
        offset += SMARTRECRUITERS_PAGE
        if not page or offset >= payload.get("totalFound", 0):
            return jobs


def _fetch_bamboohr(slug):
    return list(_normalize_bamboohr(_get(f"https://{slug}.bamboohr.com/careers/list"), slug))


TEAMTAILOR_NS = "{https://teamtailor.com/locations}"


def _fetch_teamtailor(host):
    """Teamtailor has no public JSON without an API key, but every careers site serves RSS."""
    root = ET.fromstring(_request(f"https://{host}/jobs.rss"))
    jobs = []
    for item in root.iter("item"):
        locations = [
            name.text.strip()
            for name in item.iter(f"{TEAMTAILOR_NS}name")
            if name.text and name.text.strip()
        ]
        if (item.findtext("remoteStatus") or "").strip() == "fully":
            locations.append("Remote")
        jobs.append(
            (
                (item.findtext("title") or "").strip(),
                "; ".join(locations),
                (item.findtext("link") or "").strip(),
            )
        )
    return jobs


FETCHERS = {
    "workday": _fetch_workday,
    "avature": _fetch_avature,
    "smartrecruiters": _fetch_smartrecruiters,
    "bamboohr": _fetch_bamboohr,
    "teamtailor": _fetch_teamtailor,
}


def _fetch_board(entry):
    """Return (display_name, [(title, location, url), ...], error)."""
    display, slug, ats = entry
    try:
        if ats in ENDPOINTS:
            jobs = list(NORMALIZERS[ats](_get(ENDPOINTS[ats].format(slug))))
        else:
            jobs = FETCHERS[ats](slug)
    except (urllib.error.URLError, OSError, ValueError, ET.ParseError) as exc:
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


def needs_review(title, location):
    """Looks early-career but matches no known intern wording."""
    if INTERN_PATTERN.search(title) or not EARLY_CAREER_HINT.search(title):
        return False
    if NOT_A_ROLE.search(title) or not config.is_eligible(title):
        return False
    return not NON_US.search(location or "")


# Boards that errored or came back empty on the last fetch, for the daily
# digest. One broken board otherwise looks the same as no new postings.
board_problems = []


def fetch_studio_listings():
    """Query every studio board. Individual board failures are tolerated."""
    listings, failures, scanned = [], [], 0
    board_problems.clear()

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for display, jobs, error in pool.map(_fetch_board, STUDIO_BOARDS):
            if error:
                failures.append(f"{display} ({error})")
                board_problems.append(f"{display} ({error})")
                continue
            if not jobs:
                board_problems.append(f"{display} (0 jobs on the board)")
            scanned += len(jobs)
            for title, location, url, *rest in jobs:
                description = rest[0] if rest else ""
                review = needs_review(title, location)
                if not url or not (review or is_relevant(title, location)):
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
                        needs_review=review,
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
