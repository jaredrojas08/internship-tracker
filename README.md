# Internship Tracker

Aggregates Summer 2027 internship listings from multiple public sources, filters them against one profile, deduplicates across sources, and syncs to a Notion database. Runs hourly on GitHub Actions — nothing needs to be running locally.

## Sources

| Source | Listings | Notes |
|---|---|---|
| [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships) | ~95 | markdown links; also publishes the Programs & Fellowships table (see Notion database below) |
| [speedyapply/2027-SWE-College-Jobs](https://github.com/speedyapply/2027-SWE-College-Jobs) | ~121 | HTML anchors, three subsections, publishes salary |
| Game studio ATS boards (`studios.py`) | ~20 in season | 31 boards over Greenhouse / Ashby / Lever / Workday / Avature |

Each source gets its own parse function in `sources.py`; everything downstream is source-agnostic. To add one, write a parse function returning `Listing` objects and append it to `SOURCES`.

### When a source breaks

One bad upstream must not take down the run, but degrading quietly is its own failure.

**Existing pages are never deleted.** The Notion sink only ever appends listings it hasn't seen before (`write_to_notion`); a source going down for a day just means fewer new pages that run, never fewer old ones. This is a structural difference from the old Sheets sink, which rebuilt every row from the current fetch each run and needed an explicit retention step to avoid wiping out a source's rows during an outage.

**The digest says so, for outright failures.** A source returning an error, or parsing to zero listings when it isn't supposed to be empty, produces a 🚨 warning pinned to the top of the digest, and the headline changes to `⚠️ source problem`. A source that parses successfully but returns far fewer listings than usual — a shrink rather than a failure — isn't caught: that check needs a per-source history of counts, which only the retired Sheets pipeline kept.

### Deduplication

The same job appears in more than one list under different URLs and titles. Two fingerprints run in order:

1. **URL job id** — the trailing numeric id from the ATS path, after stripping query params. Collapses Google's `.../results/85564713261245126` and `.../results/85564713261245126-software-engineering-intern/`, and vanshb03-style `?utm_source=` tracking.
2. **Company + role text** — case, punctuation, whitespace and emoji normalized away, *nothing else*.

The second is deliberately conservative. Stripping seasons and parentheticals — the obvious move — merged all of these into single rows:

| Merged incorrectly | Reality |
|---|---|
| Western Digital *Summer 2027 Intern* / *Winter 2027 Co-op* | different programs |
| Optiver *(Austin)* / *(Chicago)* | different offices |
| Kudu Dynamics *(1)* / *(2)* / *(3)* | three distinct roles |
| Jane Street *Winter Co-Op* / *Summer Internship* | different terms |

The costs aren't symmetric: a false merge hides a real posting permanently, a missed duplicate just shows up twice. It errs toward showing twice. Earlier entries in `SOURCES` win, so the list order is a priority order.

## What it does

1. Downloads each configured source's raw README
2. Parses their tables, which differ in columns, link syntax and subsection layout
3. Drops closed roles (🔒) and anything gated on a graduate degree
4. Keeps roles matching the keyword filter in `config.py`
5. Deduplicates across sources
6. Writes new listings into a Notion database, deduplicated by Job ID and never rewriting a page that already exists

Sponsorship and citizenship flags (🛂, 🇺🇸) are deliberately **kept** so you can see them.

## Notion database

The schema lives in `notion_sink.SCHEMA` — that dict is the source of truth; this table just names what each property is for.

| Property | Type | Content |
|---|---|---|
| Title | title | Role (emoji flags preserved) |
| Company | rich text | Company name |
| Category | select | e.g. game dev, backend, ML — from `enrich.category_for` |
| Term | select | Extracted from the title, e.g. "Summer 2027" |
| Location | rich text | Location as the source published it |
| Application Portal | url | Apply link |
| Resume Keywords | multi-select | Keywords `enrich` pulled from the posting |
| Skill Requirements | rich text | Requirements section scraped from the posting page |
| Posted | date | The employer's own `datePosted` when the page publishes one, otherwise the run that first saw the listing (`first_seen`). Never blank, because Hours Since Posted is what the Recent view sorts on |
| Hours Since Posted | formula | `dateBetween(now(), Posted, "hours")` — self-updating, no write needed |
| Recruiter Contact | email | Scraped from the posting page when present |
| **Applied** | select | `Not applied` (default) / `Applying` / `Applied` / `Interviewing` / `Offer` / `Rejected` |
| My Resume PDF | files | Yours to attach; the script never touches it |
| Source | select | Which list the listing came from |
| Job ID | rich text | Dedup key, derived from the apply URL. Hidden from views, not from you. **Never rename or clear this column**: a populated database that reads back zero job ids aborts the run rather than re-adding all 745 rows |
| Notes | rich text | Free text; catches deadlines the Deadline property can't hold (e.g. "rolling") |
| Deadline | date | Filled only when a page states one in machine-readable form (see below) |
| **Applied Date** | date | Stamped once, the first time a row is set to `Applied`. Never rewritten after |

**Applied**, **My Resume PDF**, and **Deadline** (when you type over it) are yours; the script reads them but never overwrites a value you set. Job ID, not row position, is how a listing is recognized across runs, so sorting or filtering the database view never breaks the sync.

Programs & Fellowships share this same database and shape: `parser.programs_to_listings` maps Organization → Company, Opportunity → Title, tags every row Category `Program / Fellowship`, and folds Type plus any non-ISO Deadline into Notes. They skip enrichment entirely, which is what keeps their Category and Notes intact, since enrichment recomputes Category from the role title and re-fetches the apply page.

### Deadline

**Measured against the live sources, roughly 3% of postings state a deadline in machine-readable form.** Job boards overwhelmingly don't publish one. So this property is primarily yours to fill in by hand; the script fills it only when a page explicitly says something like "apply by January 15, 2027" *and* the property is still blank. Anything you type wins permanently. A hand-typed value that isn't a real date (`rolling`, `check site`) has nowhere to go in a date property, so it lands in Notes instead.

A bare date on a job page is usually the start date or posting date, so extraction requires an explicit cue phrase ahead of the date and won't reach across a sentence boundary.

### Applied Date and follow-ups

Setting **Applied** to `Applied` stamps today's date into **Applied Date** on the next run. The stamp is **never cleared** — changing the status back by accident shouldn't destroy the record of when you submitted.

An application still unanswered after `FOLLOW_UP_AFTER_DAYS` (21) appears in the digest. There's no Notion equivalent of a dead or closed link — link checking isn't part of this design — so every row counts as open for that purpose.

### Daily digest

The Notion database is a pull interface — only useful when you remember to open it. The digest pushes what changed:

- new listings, with game roles called out first
- deadlines within 14 days you haven't applied to
- applications past the follow-up window
- rows marked `Applying` with no resume attached yet, so the tailoring queue stays visible

The full digest is daily. The `Applying` nag runs on its own two-hour clock, because a row marked `Applying` is waiting on you rather than on the scrape, and a day is too long to sit on that. Nagging does not consume the daily slot, and a full digest resets the nag clock so the same row is not named twice within a couple of hours. `config.NAG_INTERVAL_HOURS` is the knob.

On a quiet day it sends a one-line heartbeat instead:

```
😴 No new listings today.
214 open · 3 applied · next deadline Western Digital in 85d (2026-10-20)
```

That exists so **silence always means the run failed**, never "nothing happened" — otherwise a broken workflow is indistinguishable from a slow week. It's deliberately terse and uses a distinct emoji so it can be dismissed at a glance.

Set `NOTIFY_ON_QUIET_DAYS=false` to only hear from it when something actually changed.

**One digest a day, covering every listing since the last one.** The scrape runs hourly but the digest fires once every 24 hours, so each run parks what it found in `digest_state.json` (which the workflow commits) and the next digest to pass the gate announces the whole backlog at once. A successful send clears it; a failed send leaves it, so the retry an hour later still has something to say.

A source failure is the exception: it sends immediately, whatever the gate says. That alert carries **only** the warning, and does not start the 24-hour cooldown. Otherwise a 3am breakage notice would stand in for the day's real digest.

Verify a new channel with `--test-notify`, which sends a clearly-labelled sample and exits.

Channels are opt-in by secret; set either, both, or neither:

| Channel | Secrets |
|---|---|
| Discord | `DISCORD_WEBHOOK_URL` — create via Server Settings → Integrations → Webhooks |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `NOTIFY_EMAIL_TO` |

With neither set the digest silently no-ops. Delivery failures are logged as warnings and never fail the run — the database is already written by then. Skip with `--no-notify`.

There is no link checking in this pipeline: a listing's apply link is never re-fetched to see if the role is still open. `ats.py` fetches each posting exactly once (on the run that first sees it, before it is written), and only to scrape Skill Requirements, Recruiter Contact and the posting date, not to classify link health.

That ordering is load-bearing. Dedup against Notion runs *before* enrichment (`select_new`, then `enrich.enrich_all`), so a run that finds nothing new makes no outbound fetches at all. Enriching first meant ~735 fetches an hour to write zero pages, which is roughly 530,000 requests a month at other people's ATS servers and more Actions minutes than the free tier allows.

The one exception is backfill: up to 25 rows a run whose Skill Requirements still say "See posting" get their page re-fetched, so a transient failure heals itself. A missing Recruiter Contact never queues a row: most postings simply don't print an address, and queueing on it pinned the same few rows at the head of the queue forever.

### Game roles

`GAME_KEYWORDS` in `config.py` flags game development work — Unity, Unreal, gameplay, graphics, shaders, rendering, VR/XR, technical art. These sort above everything except brand-new listings.

Bare `engine` is deliberately **not** a keyword: it matches jet engines, search engines and rules engines far more often than game engines. `game engine` is listed in full instead.

**Reality check.** Measured across ~4,100 rows in seven public internship lists, plus 1,144 jobs pulled directly from 14 game studio job boards, there is currently **one** game-adjacent Summer 2027 internship: Brunswick's Computer Graphics Software Developer Intern. It's in the database, sorted to the top by this rule.

That is a timing artifact, not a filter problem. Quant firms and big tech post 12+ months ahead; **game studios post summer internships between September and January**. Riot, Epic and Naughty Dog simply haven't opened Summer 2027 yet.

### Studio job boards

Live in `studios.py`. Most studios expose public, unauthenticated JSON APIs through their ATS. No auth, and the `id` field is a clean dedup key:

```
https://boards-api.greenhouse.io/v1/boards/{board}/jobs
https://api.ashbyhq.com/posting-api/job-board/{board}
https://api.lever.co/v0/postings/{board}?mode=json
POST https://{tenant}.wdN.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs   (20 a page; total only on page 1)
```

EA is the exception: `jobs.ea.com` is an Avature portal with no JSON API, so its server-rendered search page is parsed for `<article>` blocks. A template change there breaks EA and nothing else.

Verified live boards:

| ATS | Boards |
|---|---|
| Greenhouse | `riotgames` `epicgames` `roblox` `sonyinteractiveentertainmentglobal` `scopely` `rockstargames` `discord` `naughtydog` `digitalextremes` `bungie` `2k` `taketwo` `nintendo` `insomniac` `gearbox` `crystaldynamics` `azragames` `zyngacareers` |
| Ashby | `supercell` `thatgamecompany` `hoyoverse` `arenanet` `seconddinner` `believer` |
| Lever | `skydance` `jamcity` `theorycraftgames` |
| Workday | `xboxgaming.wd1/Blizzard_External_Careers` `xboxgaming.wd1/External` (Activision, Raven, Sledgehammer, Demonware) `unitytech.wd1/Unity` |
| Avature | `jobs.ea.com` (EA, Respawn, Maxis, DICE, Motive) |

Probed ~300 slugs in Sept 2026. Not found anywhere obvious: Valve, Sucker Punch, Santa Monica Studio, Obsidian, CD Projekt Red, Niantic; Xbox first-party studios sit on Microsoft's custom careers site, not the xboxgaming Workday tenant. Live boards skipped on purpose: Larian, Kabam, NetEase, Avalanche, Crytek, Housemarque, Haven, King (no US roles); Twitch, VRChat (not studios); Hasbro, Warner Bros Discovery (mostly non-game roles, and the source tags everything as game work); Ubisoft on SmartRecruiters (109 postings, US ones are rare and non-engineering). Beware name collisions: `remedy`, `bethesda`, `raven`, `moonshot`, `take2`, `lightspeed`, `paradox`, `kepler`, `lockwood` resolve to unrelated companies.

Three rules specific to this source:

- **Everything from a studio board counts as a game role**, regardless of title. "Software Engineer Intern" at Riot is game work; keyword matching would miss it.
- **The role keyword filter is bypassed**, because studios use titles like "Associate Technical Designer" that a SWE-oriented keyword list rejects. The eligibility gate (no PhD/Master's) and a US-location filter still apply. Given how few game roles exist at all, a false positive from Riot costs one row while a false negative costs the job.
- **Empty is not an error.** The source is marked `allow_empty`, so months of zero results raise no health alarm. But a *total* board outage raises — otherwise infrastructure failure would be indistinguishable from the off-season. Losing more than half the boards raises too; losing one is tolerated silently, since studios change ATS occasionally.

Handshake is not an option: it's behind Cornell SSO, has no public API, and automated access violates its terms.

### Deleting a listing

Dedup is against pages currently in the database, so deleting a page in Notion just makes the listing look new again on the next run. Record the Job ID instead:

```bash
./venv/bin/python internship_tracker.py --tombstone greenhouse.io:1234567
```

That appends to `removed.json`, which the workflow commits, and those ids are never written again. The Job ID is on the row itself (the column is hidden in most views, not removed). Several at once is fine: `--tombstone a b c`.

Marking a row `Rejected` is the softer option and needs no tombstone, since the page still exists, so dedup skips it anyway.

### Sort order

The script only ever appends pages; it never reorders the database. Sorting and filtering (by Category, Term, Applied, and so on) is a Notion view you set up yourself. What arrived today is in the digest; the database itself carries no new/seen marker.

## Local setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill it in
```

`.env` needs `NOTION_TOKEN`, `NOTION_PARENT_PAGE_ID`, and `NOTION_DATABASE_ID`. Getting those three values, including the one-time integration setup, is documented in [`docs/notion-setup.md`](docs/notion-setup.md).

## Running

```bash
./venv/bin/python internship_tracker.py --notion         # fetch, filter, and sync to Notion
./venv/bin/python internship_tracker.py --create-database  # one-time: create the database, print its id
./venv/bin/python internship_tracker.py --test-notify    # send a sample digest and exit
./venv/bin/python internship_tracker.py --notion --no-notify  # sync without sending a digest
./venv/bin/python internship_tracker.py --tombstone JOB_ID  # never re-add a listing you deleted in Notion
```

There's no dry-run preview for the Notion sink: `--notion` fetches, filters, and writes in one pass. To check the effect of a filter change without writing, read the run's log output — it reports unique-listing and new-listing counts before anything is sent to Notion — or temporarily add a print in `main()`.

## GitHub Actions

Runs hourly at :20. GitHub's scheduler is best-effort and can lag under load.

Manual run: **Actions** tab → *Update Internship Listings* → **Run workflow**.

Required repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|--------|-------|
| `NOTION_TOKEN` | the integration's API token |
| `NOTION_DATABASE_ID` | the database id, from `--create-database` |

`NOTION_PARENT_PAGE_ID` is only needed locally, for the one-time `--create-database` run; the workflow never calls it and doesn't need the secret.

## Tuning the filter

Everything lives in `config.py`:

- `ROLE_KEYWORDS` — case-insensitive, matched on word boundaries with common inflections, so `platform` also catches "Platforms" and `engineer` catches "Engineered"
- `CASE_SENSITIVE_KEYWORDS` — uppercase acronyms. `IT` is here because case-insensitively it would match the English word "it"
- `requires_advanced_degree()` — drops PhD/Master's-gated roles, unless the posting also names BS/undergrad (as in "Intern (BS/MS/PhD)")

After editing, run the tests (`./venv/bin/python -m unittest discover -s tests`) — `tests/test_config.py` covers the term extractor, and a filter change is otherwise easy to verify by hand against a few titles in a shell.

## Failure behavior

- No listings from any source → refuses to write, so a total parse failure can't wipe the database
- Notion unreachable, rate limited past its retries, or otherwise erroring → the run writes nothing further, posts a 🚨 alert to Discord, and exits non-zero. Silence never means a failed sync
- The `Job ID` column renamed or emptied → the run aborts. A populated database reading back zero job ids is a schema incident, and treating it as an empty database would append every listing a second time
- Missing/renamed columns in the source table → refuses to write rather than producing garbage
- The source added an `Added` column after this was built; the parser treats it as optional
- A page that 429s or 5xxs is retried with backoff (`notion_sink.Notion._call`); one bad Notion request doesn't necessarily fail the run
- One bad row in backfill or Applied Date stamping is logged and skipped, not allowed to abort the rest of the batch
- Delivery failures in the digest are logged as warnings and never fail the run — the database is already written by then

Programs & Fellowships are re-parsed every run alongside the regular listings, reusing the sndsh404 README already downloaded for that source — no extra HTTP call on the common path. If that source failed this run, fetching the README again just for programs is attempted once more; if that also fails, the run logs a warning and skips programs for that run rather than failing the whole sync.
