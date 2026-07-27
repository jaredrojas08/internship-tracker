# Internship Tracker

Aggregates Summer 2027 internship listings from multiple public sources, filters them against one profile, deduplicates across sources, and syncs to a styled Google Sheet. Runs daily on GitHub Actions — nothing needs to be running locally.

## Sources

| Source | Listings | Notes |
|---|---|---|
| [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships) | ~95 | markdown links; also supplies the Programs tab |
| [speedyapply/2027-SWE-College-Jobs](https://github.com/speedyapply/2027-SWE-College-Jobs) | ~121 | HTML anchors, three subsections, publishes salary |

Each source gets its own parse function in `sources.py`; everything downstream is source-agnostic. To add one, write a parse function returning `Listing` objects and append it to `SOURCES`.

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
6. Writes to a styled Google Sheet, preserving your manual entries

Sponsorship and citizenship flags (🛂, 🇺🇸) are deliberately **kept** so you can see them.

## Sheet layout

**Tab: Internship Listings**

| Col | Content |
|-----|---------|
| A | Company |
| B | Role (emoji flags preserved) |
| C | Location |
| D | Apply link |
| E | Salary — where the source publishes it |
| F | **Deadline** — mostly yours to fill in (see below) |
| G | Date Added — when the script first saw it |
| H | Remote? |
| I | Game? — game development role |
| J | Status — `NEW` for 3 days, then `SEEN` |
| K | Link Status — `OPEN` / `CLOSED` / `DEAD` / `UNKNOWN` |
| L | Last Checked — when the link was last verified |
| M | Source — which list it came from |
| N | **Applied?** — yours to toggle |
| O | Applied Date — auto-stamped when you tick Applied? |
| P | **Remove?** — check to delete the row |

Both tabs are native Google Sheets Tables, so you get per-column filter dropdowns for free. The Strawberry Kiss palette is applied through the table's own header and banding colors rather than conditional formatting.

**Tab: Programs & Fellowships** — org, opportunity, link, type, deadline, date added, plus **Applied?** and **Remove?** checkboxes with the same semantics as the listings tab. Keyed on organization + opportunity.

Removed programs tombstone to a separate hidden **Removed Programs** tab rather than the listings one, since programs key on two fields and listings on three. A program whose Deadline cell holds a real date greys out once it has passed; most are free text like `rolling` or `check site`, so that rule is guarded on `ISNUMBER`.

**Tabs: Removed / Removed Programs** (hidden) — tombstones. Without this a removed row would be re-added on the next run, since the script would no longer see it in the sheet and would treat it as new. To un-remove something, delete its row here.

### The columns you own

`Applied?`, `Remove?` and `Deadline` are never overwritten. The script identifies each listing by `Company + Role + Apply URL`, not by row number, so it re-sorts the whole sheet every run without your entries drifting onto the wrong listing.

Check `Remove?` on anything you don't want. It disappears on the next run and won't come back.

### Deadline

**Measured against the live sources, roughly 3% of postings state a deadline in machine-readable form.** Job boards overwhelmingly don't publish one. So this column is primarily yours to fill in by hand; the script fills it only when a page explicitly says something like "apply by January 15, 2027" *and* the cell is still blank. Anything you type wins permanently.

A bare date on a job page is usually the start date or posting date, so extraction requires an explicit cue phrase ahead of the date and won't reach across a sentence boundary.

Deadlines within the next 14 days highlight in dusty rose. A passed deadline greys the row out and sinks it, whether the script found it or you typed it.

### Applied Date and follow-ups

Ticking `Applied?` stamps today's date into `Applied Date` on the next run. The stamp is **never cleared** — unticking the box by accident shouldn't destroy the record of when you submitted.

An application still unanswered after `FOLLOW_UP_AFTER_DAYS` (21) highlights and appears in the digest. Roles whose link has since gone `DEAD` or `CLOSED` are excluded: those aren't waiting on a reply, they're over.

### Daily digest

The sheet is a pull interface — only useful when you remember to open it. The digest pushes what changed:

- new listings, with game roles called out first
- deadlines within 14 days you haven't applied to
- applications past the follow-up window
- roles you applied to whose posting has since closed

On a quiet day it sends a one-line heartbeat instead:

```
😴 No new listings today.
214 open · 3 applied · next deadline Western Digital in 85d (2026-10-20)
```

That exists so **silence always means the run failed**, never "nothing happened" — otherwise a broken workflow is indistinguishable from a slow week. It's deliberately terse and uses a distinct emoji so it can be dismissed at a glance.

Set `NOTIFY_ON_QUIET_DAYS=false` to only hear from it when something actually changed.

Verify a new channel with `--test-notify`, which sends a clearly-labelled sample and exits.

Channels are opt-in by secret; set either, both, or neither:

| Channel | Secrets |
|---|---|
| Discord | `DISCORD_WEBHOOK_URL` — create via Server Settings → Integrations → Webhooks |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `NOTIFY_EMAIL_TO` |

With neither set the digest silently no-ops. Delivery failures are logged as warnings and never fail the run — the sheet is already written by then. Skip with `--no-notify`.

### Link checking

Every run fetches each application URL and classifies it:

| Value | Meaning | Reliability |
|-------|---------|-------------|
| `OPEN` | 200, no closure text found | good, but a live page can still be a closed role |
| `CLOSED` | page says it's no longer accepting applications | high — the phrase list is deliberately specific |
| `DEAD` | HTTP 404 or 410 | high |
| `UNKNOWN` | JS-only page, bot-blocked, or timed out | no signal either way |

**Nothing is ever auto-deleted.** Dead and closed listings sink to the bottom of the sheet and grey out, so a misclassification costs you a glance rather than a listing. Network errors and timeouts return `UNKNOWN`, never `DEAD` — a blip should not condemn a live posting.

The honest limitation: a `200` does not prove a role is still open. Greenhouse, Lever and Ashby 404 properly when a job closes, so detection is reliable there. Workday and iCIMS ship a JavaScript shell with no readable text, which is why they come back `UNKNOWN` rather than being guessed at.

**Cadence:** each listing is re-checked every **7 days**, not every run — tracked per row in `Last Checked`. A listing the script has never seen is checked immediately, so new arrivals are always verified on arrival. This keeps the daily run fast and avoids hitting the job boards hundreds of times a day.

Skip it with `--skip-links`, or force a full sweep now with `--force-links`.

### Game roles

`GAME_KEYWORDS` in `config.py` flags game development work — Unity, Unreal, gameplay, graphics, shaders, rendering, VR/XR, technical art. These sort above everything except brand-new listings and get the deep-berry highlight.

Bare `engine` is deliberately **not** a keyword: it matches jet engines, search engines and rules engines far more often than game engines. `game engine` is listed in full instead.

**Reality check.** Measured across ~4,100 rows in seven public internship lists, plus 1,144 jobs pulled directly from 14 game studio job boards, there is currently **one** game-adjacent Summer 2027 internship: Brunswick's Computer Graphics Software Developer Intern. It's on the sheet, sorted to the top by this rule.

That is a timing artifact, not a filter problem. Quant firms and big tech post 12+ months ahead; **game studios post summer internships between September and January**. Riot, Epic and Naughty Dog simply haven't opened Summer 2027 yet.

### Planned: studio job boards

Game studios expose public, unauthenticated JSON APIs through their ATS. No scraping, no auth, and the `id` field is a clean dedup key:

```
https://boards-api.greenhouse.io/v1/boards/{board}/jobs
https://api.ashbyhq.com/posting-api/job-board/{board}
https://api.lever.co/v0/postings/{board}?mode=json
```

Verified live boards:

| ATS | Boards |
|---|---|
| Greenhouse | `riotgames` `epicgames` `roblox` `sonyinteractiveentertainmentglobal` `scopely` `rockstargames` `discord` `naughtydog` `digitalextremes` `bungie` |
| Ashby | `supercell` `thatgamecompany` |
| Lever | `skydance` `jamcity` |

Not found on these three (different ATS, needs identifying): Sucker Punch, Santa Monica Studio, 343, Obsidian, Larian, CD Projekt Red, Gearbox, Zynga, King, Niantic, Behaviour, Unity, Valve, Respawn, Blizzard, Activision, EA.

**Worth building in late August**, so it's running before the September–January window. Building it earlier means maintaining board slugs against an empty result set.

Handshake is not an option: it's behind Cornell SSO, has no public API, and automated access violates its terms.

### Sort order

Applicable first, then `NEW`, then **game**, then remote, then newest, then alphabetical by company.

New listings surface at the top for three days regardless of location; after that the sheet settles into remote-at-top. This is why row order changes between runs — the sheet is rebuilt each time, not appended to.

## Local setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill it in
```

`.env` needs:

- `GOOGLE_SHEETS_CREDENTIALS` — path to your service account JSON
- `GOOGLE_SHEET_ID` — the string in the Sheet URL between `/d/` and `/edit`

### Google Cloud credentials

1. [console.cloud.google.com](https://console.cloud.google.com) → new project
2. Enable the **Google Sheets API** and **Google Drive API**
3. Credentials → Create credentials → **Service account**. Skip the role step — it needs no project role
4. Service account → Keys → Add Key → JSON → download
5. Store it **outside this repo**. `.gitignore` blocks `service-account*.json` and `*-credentials.json`, but the safest place is somewhere else entirely
6. Share your Sheet with the service account's `client_email` as **Editor**

## Running

```bash
./venv/bin/python internship_tracker.py --dry-run   # preview, writes nothing
./venv/bin/python internship_tracker.py             # sync
./venv/bin/python internship_tracker.py --no-style   # skip formatting
./venv/bin/python internship_tracker.py --skip-links  # skip link checking
./venv/bin/python internship_tracker.py --force-links # re-check every link now
./venv/bin/python internship_tracker.py --test-notify  # send a sample digest and exit
./venv/bin/python internship_tracker.py --no-notify    # skip the digest
```

`--dry-run` is fully read-only: it won't create tabs or modify a single cell.

## GitHub Actions

Runs daily at 18:00 UTC (2 PM ET in summer, 1 PM in winter). GitHub's scheduler is best-effort and can lag by several minutes under load.

Manual run: **Actions** tab → *Update Internship Listings* → **Run workflow**.

Required repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|--------|-------|
| `GOOGLE_SHEETS_CREDENTIALS` | the entire JSON key file contents |
| `GOOGLE_SHEET_ID` | the spreadsheet ID |

The same env var holds a file path locally and raw JSON in CI; the script detects which.

## Tuning the filter

Everything lives in `config.py`:

- `ROLE_KEYWORDS` — case-insensitive, matched on word boundaries with common inflections, so `platform` also catches "Platforms" and `engineer` catches "Engineered"
- `CASE_SENSITIVE_KEYWORDS` — uppercase acronyms. `IT` is here because case-insensitively it would match the English word "it"
- `requires_advanced_degree()` — drops PhD/Master's-gated roles, unless the posting also names BS/undergrad (as in "Intern (BS/MS/PhD)")

After editing, check the effect before syncing:

```bash
./venv/bin/python internship_tracker.py --dry-run
```

## Failure behavior

- Network failure → logs and exits non-zero without touching the sheet
- Missing/renamed columns in the source table → refuses to write rather than producing garbage
- The source added an `Added` column after this was built; the parser treats it as optional
- Formatting errors are caught after data is written, so a styling failure can't cost you listings
- Link-check failures degrade to `UNKNOWN` per URL; one bad host can't fail the run

### Three traps worth recording

**`deleteTable` deletes the table _and its data rows_.** Rebuilding the table each run by delete-then-add silently emptied every text column while leaving the checkboxes behind. `styling.py` uses `updateTable` to resize in place and never issues `deleteTable`.

**A Table's BOOLEAN column coerces whatever you write into it.** Inserting `Salary` and `Source` shifted the checkbox columns, but the existing Table still declared BOOLEAN at the *old* positions — so writing rows turned `Last Checked` and `Source` into `FALSE` on 121 rows. `styling.sync_table_schema` now runs **before** the data write, not after.

**Header rewrites must migrate data.** `read_listing_state` derives column positions from row 1 of the sheet. If the header is rewritten to a new layout before state is read, new positions get mapped onto old rows and every column shifts — silently, and the corruption then feeds itself on the next run. `_migrate_columns` remaps existing rows by column *name* whenever the header changes, so adding or reordering a column is safe.
