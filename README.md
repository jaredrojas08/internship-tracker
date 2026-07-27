# Internship Tracker

Fetches Summer 2027 internship listings from [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships), filters them, and syncs them to a Google Sheet. Runs daily on GitHub Actions — nothing needs to be running locally.

## What it does

1. Downloads the source repo's raw README
2. Parses the `## the list` and `## programs open now` markdown tables
3. Drops closed roles (🔒) and anything gated on a graduate degree
4. Keeps roles matching the keyword filter in `config.py`
5. Writes to a styled Google Sheet, preserving your manual checkboxes

Sponsorship and citizenship flags (🛂, 🇺🇸) are deliberately **kept** so you can see them.

## Sheet layout

**Tab: Internship Listings**

| Col | Content |
|-----|---------|
| A | Company |
| B | Role (emoji flags preserved) |
| C | Location |
| D | Apply link |
| E | **Deadline** — mostly yours to fill in (see below) |
| F | Date Added — when the script first saw it |
| G | Remote? |
| H | Status — `NEW` for 3 days, then `SEEN` |
| I | Link Status — `OPEN` / `CLOSED` / `DEAD` / `UNKNOWN` |
| J | Last Checked — when the link was last verified |
| K | **Applied?** — yours to toggle |
| L | **Remove?** — check to delete the row |

Both tabs are native Google Sheets Tables, so you get per-column filter dropdowns for free. The Strawberry Kiss palette is applied through the table's own header and banding colors rather than conditional formatting.

**Tab: Programs & Fellowships** — org, opportunity, link, type, deadline, date added.

**Tab: Removed** (hidden) — tombstones. Without this a removed row would be re-added on the next run, since the script would no longer see it in the sheet and would treat it as new. To un-remove something, delete its row here.

### The columns you own

`Applied?`, `Remove?` and `Deadline` are never overwritten. The script identifies each listing by `Company + Role + Apply URL`, not by row number, so it re-sorts the whole sheet every run without your entries drifting onto the wrong listing.

Check `Remove?` on anything you don't want. It disappears on the next run and won't come back.

### Deadline

**Measured against the live source, only 3 of 95 postings state a deadline in machine-readable form — about 3%.** Job boards overwhelmingly don't publish one. So this column is primarily yours to fill in by hand; the script fills it only when a page explicitly says something like "apply by January 15, 2027" *and* the cell is still blank. Anything you type wins permanently.

A bare date on a job page is usually the start date or posting date, so extraction requires an explicit cue phrase ahead of the date and won't reach across a sentence boundary.

Deadlines within the next 14 days highlight in dusty rose. A passed deadline greys the row out and sinks it, whether the script found it or you typed it.

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

**Cadence:** each listing is re-checked every **7 days**, not every run — tracked per row in `Last Checked`. A listing the script has never seen is checked immediately, so new arrivals are always verified on arrival. This keeps the daily run fast and avoids hitting the job boards 95 times a day.

Skip it with `--skip-links`, or force a full sweep now with `--force-links`.

### Sort order

Applicable first, then `NEW`, then remote, then newest, then alphabetical by company.

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

### Two traps worth recording

**`deleteTable` deletes the table _and its data rows_.** Rebuilding the table each run by delete-then-add silently emptied every text column while leaving the checkboxes behind. `styling.py` uses `updateTable` to resize in place and never issues `deleteTable`.

**Header rewrites must migrate data.** `read_listing_state` derives column positions from row 1 of the sheet. If the header is rewritten to a new layout before state is read, new positions get mapped onto old rows and every column shifts — silently, and the corruption then feeds itself on the next run. `_migrate_columns` remaps existing rows by column *name* whenever the header changes, so adding or reordering a column is safe.
