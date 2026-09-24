# Notion migration

Replace the Google Sheets output with a Notion database modelled on
`maiaschlesiger/internships`, keeping this repo's sources, filters and Discord
digest. Written 2026-09-24.

## Why

The Sheet has carried this project since July, and three of its limits now cost
real work. It has no file attachment, so tailored resumes live somewhere else
with no link back to the listing they were written for. Its Applied dropdown is
a text cell dressed up with conditional formatting, and the colors are invisible
to the API, so a styling change can silently erase them. And every column
position is load-bearing: the BOOLEAN coercion bug in July destroyed two columns
on 121 rows without raising an error.

Notion gives a files property, a real select, and named properties that do not
shift when a column is added. Maia's repo already solves the same problem for
the same kind of search, so the design is borrowed rather than invented.

## Scope

In scope: the listings pipeline and the programs table move to Notion. Out of
scope: the sources, the role filters, the resume tailoring workflow, and the
`resume-tailor` skill, all of which stay exactly as they are.

## Decisions

These were settled in conversation on 2026-09-24 and are not open questions.

| Decision | Choice |
|---|---|
| Google Sheet | Retired. One final CSV export committed, then no further writes. |
| Anthropic API key | Not used. Verified in Maia's code that the pipeline degrades to verbatim requirement text and keyword-only classification. Resume tailoring stays manual through the `resume-tailor` skill. |
| Apollo | Not used. |
| Schedule | Hourly, matching Maia. Private repo, so roughly 1,100 of 2,000 free Actions minutes per month. |
| Discord digest | Kept, batched to one a day plus immediate source-failure alerts. |
| Link checking | **Dropped.** `linkcheck.py` and the Link Status / Last Checked columns go. |
| Deadline | Kept as a real Notion date property. |
| Schema | Maia's exactly, plus Deadline. |

Dropping the link checker means a dead posting stays in the table looking live.
`Hours Since Posted` partly covers it, since a posting that has been up for a
month is usually gone, but the signal is weaker than an HTTP check. This is a
deliberate trade for a schema closer to Maia's.

## Architecture

The pipeline is already `sources -> dedupe -> filter -> sink`, the same shape as
Maia's, so this is a sink replacement plus two new stages.

```
sources.py / studios.py        (unchanged: 31 boards, 5 ATSes, 2 GitHub lists)
        |
        v
  ats.py  (new: merges studios.py's ATS clients with Maia's jobdesc.py)
        |                       fetches each new listing's posting text
        v
 enrich.py (new: verbatim requirements, recruiter email, resume keywords)
        |
        v
 sources.deduplicate           (unchanged)
        |
        v
 notion_sink.py (new)          replaces sheets.py + styling.py
        |
        v
 notify.py                     (unchanged behaviour, new trigger cadence)
```

### Modules

**Kept unchanged:** `sources.py`, `studios.py`, `parser.py`, `config.py`.

**Kept, one change:** `notify.py`. Its digest content is unchanged, but hourly
runs mean it can no longer fire every run. It sends one digest a day covering
everything seen since the last one, and fires immediately for a source failure.

**Retired:** `sheets.py` (479 lines), `styling.py` (477), `linkcheck.py` (290).

**New `ats.py`.** `studios.py` already speaks Greenhouse, Ashby, Lever, Workday
and Avature to list jobs; Maia's `jobdesc.py` speaks the same protocols to fetch
one job's description. Keeping both would mean two Workday clients drifting
apart. `ats.py` owns the protocol layer: given a URL, return the posting text.
`studios.py` keeps only its board list and its relevance rules.

**New `enrich.py`.** Ported from Maia minus the model path. Lifts the
requirements section verbatim from the fetched page, scrapes any hiring address
printed in the posting, and falls back to a per-category keyword list when a
page cannot be read. A row whose page failed says "See posting" rather than
guessing from the title.

**New `notion_sink.py`.** Ported from Maia. Creates the database on first run,
adds any missing column on later runs without renaming or removing anything,
and writes one page per listing keyed on Job ID.

### Data flow per run

1. Fetch and parse every source. Unchanged.
2. Deduplicate across sources. Unchanged.
3. Read every existing page from Notion, building a Job ID set.
4. For listings not already present, fetch the posting page and enrich.
5. Write new pages. Skip pages that already exist rather than rewriting them,
   so annotations are never overwritten.
6. Backfill up to 25 existing rows per run that are missing skill requirements
   or a recruiter contact, newest first.
7. Post the Discord digest once a day, or immediately on a source failure.

## The Notion database

Maia's schema, with her select options and colors preserved.

| Property | Type | Notes |
|---|---|---|
| Title | title | Role name, hyperlinked to the listing |
| Company | rich_text | |
| Category | select | Carries what `Game?` did: Game Programming, Software Engineering, and the rest of `config.py`'s categories |
| Term | select | |
| Location | rich_text | |
| Application Portal | url | Redirects followed to the employer's own ATS page where resolvable |
| Resume Keywords | multi_select | |
| Skill Requirements | rich_text | Verbatim from the posting's requirements section |
| Posted | date | See estimation below |
| Hours Since Posted | formula | `dateBetween(now(), prop("Posted"), "hours")`, recalculated on view |
| Recruiter Contact | email | Scraped from the posting only, never constructed |
| Applied | select | Not applied / Applying (yellow) / Applied (blue) / Interviewing (purple) / Offer (green) / Rejected (red) |
| My Resume PDF | files | Drag a tailored PDF onto the row |
| Source | select | |
| Job ID | rich_text | Dedup key, never delete this column |
| Notes | rich_text | Pay, programme length, GPA cut-off, sponsorship and citizenship flags, quoted not inferred |
| Niche | checkbox | True when no mainstream aggregator carried the listing |
| **Deadline** | **date** | **The one addition to Maia's schema** |

`Niche` is ticked for every listing that came from a studio board, since no
GitHub aggregator carries those. Filtering the table on it gives the roles the
big lists never surface, which is most of the game work.

Sorting moves out of the code. `sheets.py` re-sorted all 682 rows on every run;
Notion saved views do this without a write. Three views ship with the database:
Game roles first, Applying (the tailoring queue), and Recent.

### Posting time

Three estimates, most precise first, matching Maia:

1. `scraped` — the employer's own `datePosted` from the schema.org JobPosting
   block, when it carries a time of day.
2. `commit` — for the GitHub list sources, the commit that first introduced the
   row. Roughly 70 minutes of precision.
3. `first_seen` — the run that first observed it, accurate to the cron interval.

A bare calendar date never replaces a better estimate.

## Programs and fellowships

The Sheet's second tab holds 10 programs. Rather than a second database they
fold into the listings database with `Category = "Program / Fellowship"`:
Organization maps to Company, Opportunity to Title, and the program's Type to
Notes. One place to look, which is the point of the move.

## Deletion

Maia's dedup is against pages currently in the database, so a row deleted in
Notion reappears on the next run. This repo already solves that with tombstone
tabs and must keep solving it.

A deleted or archived page's Job ID is written to `removed.json`, committed by
the workflow. The keepalive workflow already pushes commits from Actions, so the
mechanism is proven. Job IDs in that file are never re-added.

Marking a row `Rejected` is the softer option and needs no tombstone, since the
page still exists and dedup skips it.

## Migration

One `--migrate` run, executed once:

1. Read all 682 listing rows and 10 program rows from the Sheet.
2. Write each into Notion, preserving Date Added as `Posted` with estimate
   `first_seen`, the 9 hand-entered deadlines, and the Booz Allen row's
   `Applying` status.
3. Export the Sheet to CSV and commit it under `docs/` as an archive.
4. Stop writing to the Sheet. It stays readable in Drive indefinitely.

Bootstrap then runs once to improve `Posted` where commit history offers a
better estimate.

## Error handling

Per-source failures already degrade rather than abort, and that stays. New
failure modes:

- **Notion unreachable or rate limited.** Notion allows ~3 requests/second.
  Writes are throttled and retried with backoff, the same treatment the Sheets
  client got for transient 5xx. A failed run writes nothing and alerts Discord.
- **Posting page unreadable.** Dead link, login wall, or JS-only with no
  embedded payload. The row is still written; Skill Requirements says
  "See posting". Never a run failure.
- **Schema drift.** Every run checks the database's properties and adds what is
  missing. Nothing is renamed or removed, so columns added by hand survive.
- **Partial write.** Each listing is one page write. A failure part way through
  leaves earlier pages written and is picked up next run by dedup.

## Testing

This repo currently has no tests, and the Sheets bugs in July are the argument
for adding them here rather than later.

- Port Maia's parser fixtures and her 15 regression tests where they apply.
- Notion writer tested against a fake client: asserts property names and types
  match the schema, that an existing Job ID is skipped rather than rewritten,
  and that a tombstoned Job ID is never written.
- `ats.py` tested against saved payloads for each of the five ATSes, so a
  provider changing shape fails a test rather than a run.
- Migration tested on a scratch Notion database before it touches the real one.

## Rollout

1. Build behind `--notion`, with the Sheets path still working.
2. Run both for one hourly cycle and compare row counts and field values.
3. Migrate, verify by reading the database back.
4. Delete the Sheets modules in a separate commit, so the revert is clean.

## Open question

None blocking. `intern-list.com` and `hiringcafe` exist in Maia's repo and are
not adopted here; this repo's sources already cover US SWE and game roles, and
adding sources is a separate decision with its own measurement (see the
`internship-source-candidates` note).
