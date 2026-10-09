"""Notion database sink: schema creation, dedup, and row append.

Auth is an internal integration token (``NOTION_TOKEN``). Create one at
notion.so/my-integrations, then share the target page/database with it -- an
integration can only see what has been explicitly shared, which is the most
common reason this module 404s on a page that plainly exists.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Sequence, Set, Tuple

import requests

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"

# Property names are the single source of truth for this database. Changing a
# name here without changing it in Notion creates a duplicate column.
P_TITLE = "Title"
P_COMPANY = "Company"
P_CATEGORY = "Category"
P_TERM = "Term"
P_LOCATION = "Location"
P_PORTAL = "Application Portal"
P_KEYWORDS = "Resume Keywords"
P_SKILLS = "Skill Requirements"
P_POSTED = "Posted"
P_AGE = "Hours Since Posted"
P_RECRUITER = "Recruiter Contact"
P_APPLIED = "Applied"
P_RESUME = "My Resume PDF"
P_SOURCE = "Source"
P_JOB_ID = "Job ID"
P_NOTES = "Notes"
P_DEADLINE = "Deadline"
P_APPLIED_DATE = "Applied Date"

APPLIED_OPTIONS = [
    {"name": "Not applied", "color": "default"},
    {"name": "Applying", "color": "yellow"},
    {"name": "Applied", "color": "blue"},
    {"name": "Interviewing", "color": "purple"},
    {"name": "Offer", "color": "green"},
    {"name": "Rejected", "color": "red"},
    # Wrong field or out of reach. Kept as a row so the next run does not re-add it.
    {"name": "Skipped", "color": "gray"},
]
SKIPPED = "Skipped"

SCHEMA = {
    P_TITLE: {"title": {}},
    P_COMPANY: {"rich_text": {}},
    P_CATEGORY: {"select": {}},
    P_TERM: {"select": {}},
    P_LOCATION: {"rich_text": {}},
    P_PORTAL: {"url": {}},
    P_KEYWORDS: {"multi_select": {}},
    P_SKILLS: {"rich_text": {}},
    P_POSTED: {"date": {}},
    # Notion re-evaluates now() when the page is viewed, which is what makes
    # this column self-updating without any scheduled write.
    P_AGE: {"formula": {"expression": f'dateBetween(now(), prop("{P_POSTED}"), "hours")'}},
    P_RECRUITER: {"email": {}},
    P_APPLIED: {"select": {"options": APPLIED_OPTIONS}},
    P_RESUME: {"files": {}},
    P_SOURCE: {"select": {}},
    P_JOB_ID: {"rich_text": {}},
    P_NOTES: {"rich_text": {}},
    P_DEADLINE: {"date": {}},
    # Stamped once, the first time a row is seen as Applied. Never rewritten
    # after that -- see stamp_applied_date and rows_missing_applied_date.
    P_APPLIED_DATE: {"date": {}},
}


def _select(value: str) -> str:
    """Make a string safe as a Notion select option.

    Notion rejects a select option containing a comma, with a 400 that fails
    the whole page write. Merged values are the ones that hit this -- a job
    found in several source lists carries all their names -- so the listings
    most worth having were the ones being dropped.
    """
    return (value or "").replace(",", " +")[:100]


def _plain_text(chunks) -> str:
    """Flatten a Notion rich_text / title property to a plain string."""
    return "".join(c.get("plain_text", "") for c in (chunks or [])).strip()


class Notion:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        })

    def _call(self, method: str, path: str, **kw) -> dict:
        """One request with retry on Notion's 429, transient 5xx, and timeouts."""
        # Creating a page or database is not safe to repeat: Notion may have
        # made it before the response was lost, and a retry would duplicate it.
        repeatable = not (method == "POST" and path in ("/pages", "/databases"))
        for attempt in range(5):
            try:
                resp = self.s.request(method, f"{API}{path}", timeout=30, **kw)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if not repeatable or attempt == 4:
                    raise
                log.warning("notion %s -> %s, retrying in %ds", path, type(exc).__name__, 2 ** attempt)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("notion %s -> %s, retrying in %.0fs", path, resp.status_code, wait)
                time.sleep(wait)
                continue
            if not resp.ok:
                raise RuntimeError(f"Notion {method} {path} -> {resp.status_code}: {resp.text[:400]}")
            return resp.json()
        raise RuntimeError(f"Notion {method} {path} kept failing after retries")

    def create_database(self, parent_page_id: str, title: str) -> str:
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": SCHEMA,
        }
        db = self._call("POST", "/databases", json=payload)
        log.info("created database %s (%s)", title, db["id"])
        return db["id"]

    def ensure_schema(self, database_id: str) -> List[str]:
        """Add any columns this code expects that the database does not have.

        A database created by an older version is missing newer columns, and
        writing to a property Notion does not know about fails the whole page.
        Only additions are made -- nothing existing is renamed or removed, so
        columns you added yourself are safe.
        """
        db = self._call("GET", f"/databases/{database_id}")
        have = set(db.get("properties", {}))
        missing = {name: spec for name, spec in SCHEMA.items() if name not in have}
        if not missing:
            return []
        self._call("PATCH", f"/databases/{database_id}", json={"properties": missing})
        log.info("added missing columns: %s", ", ".join(sorted(missing)))
        return sorted(missing)

    def existing_job_ids(self, database_id: str) -> Set[str]:
        """Every Job ID already stored in the database.

        Job ID is the dedup key: it is derived from the listing's own URL, so
        it stays stable across runs for a given posting.
        """
        ids: Set[str] = set()
        rows_seen = 0
        cursor: Optional[str] = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            page = self._call("POST", f"/databases/{database_id}/query", json=body)
            for row in page.get("results", []):
                rows_seen += 1
                props = row.get("properties", {})
                text = _plain_text(props.get(P_JOB_ID, {}).get("rich_text", []))
                if text:
                    ids.add(text)
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
        # A populated database with no readable job ids means the column was
        # renamed or cleared, not that the database is empty. Treating it as
        # empty would append every listing again as new.
        if rows_seen and not ids:
            raise RuntimeError(
                f"{rows_seen} row(s) in the database but none carry a {P_JOB_ID!r} "
                "value: the dedup column has been renamed or emptied. Refusing to "
                "run rather than re-adding every listing."
            )
        log.info("database already holds %d job ids", len(ids))
        return ids

    def existing_title_fingerprints(self, database_id: str) -> Dict[tuple, str]:
        """Company-and-title fingerprint of every row, mapped to its Source."""
        import sources
        out: Dict[tuple, str] = {}
        cursor: Optional[str] = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            page = self._call("POST", f"/databases/{database_id}/query", json=body)
            for row in page.get("results", []):
                props = row.get("properties", {})
                fp = sources.title_fingerprint(
                    _plain_text(props.get(P_COMPANY, {}).get("rich_text", [])),
                    _plain_text(props.get(P_TITLE, {}).get("title", [])))
                if fp:
                    out[fp] = (props.get(P_SOURCE, {}).get("select") or {}).get("name", "")
            if not page.get("has_more"):
                return out
            cursor = page.get("next_cursor")

    def rows_to_backfill(self, database_id: str, limit: int = 25) -> List[dict]:
        """Rows still missing any enrichment field, oldest first.

        Oldest first on purpose. Newest first pinned the queue to a block of
        rows whose pages cannot be read at all (programme landing pages, and
        studio boards that publish a JavaScript vanity URL), so the same 25
        came back every hour and the rest never got a turn.
        """
        out: List[dict] = []
        cursor: Optional[str] = None
        while True:
            body: dict = {
                "page_size": 100,
                "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
            }
            if cursor:
                body["start_cursor"] = cursor
            page = self._call("POST", f"/databases/{database_id}/query", json=body)
            for row in page.get("results", []):
                props = row.get("properties", {})
                if (props.get(P_APPLIED, {}).get("select") or {}).get("name") == SKIPPED:
                    continue
                needs = {
                    "needs_skills": not _plain_text(props.get(P_SKILLS, {}).get("rich_text", [])),
                    "needs_recruiter": not props.get(P_RECRUITER, {}).get("email"),
                    "needs_keywords": not props.get(P_KEYWORDS, {}).get("multi_select"),
                    "needs_notes": not _plain_text(props.get(P_NOTES, {}).get("rich_text", [])),
                }
                # Only a missing Skill Requirements earns a fetch. A recruiter
                # address is rarely published and notes are often genuinely
                # absent, so queueing on those would hold every row in the
                # queue forever. The others ride along once the page is open.
                if not needs["needs_skills"]:
                    continue
                out.append({
                    "page_id": row["id"],
                    "url": props.get(P_PORTAL, {}).get("url") or "",
                    "company": _plain_text(props.get(P_COMPANY, {}).get("rich_text", [])),
                    "role": _plain_text(props.get(P_TITLE, {}).get("title", [])),
                    **needs,
                })
                if limit and len(out) >= limit:
                    return out
            if not page.get("has_more"):
                return out
            cursor = page.get("next_cursor")

    def rows_for_digest(self, database_id: str) -> Tuple[List[dict], int, int]:
        """Rows a digest could act on, plus the real database-wide totals.

        Returns (rows, total_count, applied_count). Only Deadline-or-Applied
        rows come back in `rows` -- enough for the "coming up" and follow-up
        sections -- but the summary line ("N open * M applied") needs the true
        counts across the whole database, not just this filtered slice, and
        this method already pages through every row to build `rows`, so
        counting costs nothing extra. Link Status has no Notion equivalent --
        link checking isn't part of this design -- so nothing here is ever
        "closed"; every row counts as open.
        """
        out: List[dict] = []
        total = 0
        applied_count = 0
        cursor: Optional[str] = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            page = self._call("POST", f"/databases/{database_id}/query", json=body)
            for row in page.get("results", []):
                props = row.get("properties", {})
                deadline = (props.get(P_DEADLINE, {}).get("date") or {}).get("start", "")
                applied = (props.get(P_APPLIED, {}).get("select") or {}).get("name", "")
                total += 1
                if applied == "Applied":
                    applied_count += 1
                if applied == SKIPPED:
                    continue
                if not deadline and applied not in ("Applied", "Applying"):
                    continue
                out.append({
                    "Company": _plain_text(props.get(P_COMPANY, {}).get("rich_text", [])),
                    "Role": _plain_text(props.get(P_TITLE, {}).get("title", [])),
                    "Deadline": deadline,
                    "Application": applied,
                    "Applied Date": (props.get(P_APPLIED_DATE, {}).get("date") or {}).get("start", ""),
                    "Has Resume": bool(props.get(P_RESUME, {}).get("files")),
                })
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
        return out, total, applied_count

    def rows_missing_applied_date(self, database_id: str) -> List[dict]:
        """Pages marked Applied that have never had Applied Date stamped.

        A row that already carries a date is never returned, even if Applied
        later flips away and back -- the first stamp is permanent.
        """
        out: List[dict] = []
        cursor: Optional[str] = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            page = self._call("POST", f"/databases/{database_id}/query", json=body)
            for row in page.get("results", []):
                props = row.get("properties", {})
                applied = (props.get(P_APPLIED, {}).get("select") or {}).get("name", "")
                has_date = bool((props.get(P_APPLIED_DATE, {}).get("date") or {}).get("start"))
                if applied == "Applied" and not has_date:
                    out.append({"page_id": row["id"]})
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
        return out

    def stamp_applied_date(self, page_id: str, applied_date: str) -> None:
        """Set Applied Date. Touches no other property on the page.

        Kept separate from update_row so that method's guarantee -- it can
        only ever touch Skill Requirements and Recruiter Contact -- stays
        true and easy to verify.
        """
        self._call("PATCH", f"/pages/{page_id}", json={
            "properties": {P_APPLIED_DATE: {"date": {"start": applied_date}}},
        })
    def update_row(self, page_id: str, skills: str = "", recruiter: str = "",
                   keywords: Optional[Sequence[str]] = None, notes: str = "") -> None:
        """Fill in enrichment fields on an existing page.

        The signature is the allowlist: these four names are the only
        properties this method can ever write. Applied, Applied Date,
        Deadline and My Resume PDF belong to Jared, and backfill exists
        precisely so a repair run cannot overwrite them.
        """
        props: Dict[str, dict] = {}
        if skills:
            props[P_SKILLS] = {"rich_text": [{"type": "text", "text": {"content": skills[:2000]}}]}
        if recruiter:
            props[P_RECRUITER] = {"email": recruiter}
        if keywords:
            props[P_KEYWORDS] = {"multi_select": [
                {"name": k.replace(",", " ")[:100]} for k in keywords[:25]
            ]}
        if notes:
            props[P_NOTES] = {"rich_text": [{"type": "text", "text": {"content": notes[:2000]}}]}
        if not props:
            return
        self._call("PATCH", f"/pages/{page_id}", json={"properties": props})

    def add(self, database_id: str, listing) -> None:
        def rt(value: str) -> dict:
            return {"rich_text": [{"type": "text", "text": {"content": value[:2000]}}]} if value else {"rich_text": []}

        # The title links to the posting, and Application Portal repeats it as
        # a plain url so a Notion view can show the link as its own column.
        title_text = {"content": listing.role[:2000]}
        if listing.apply_url:
            title_text["link"] = {"url": listing.apply_url}

        props = {
            P_TITLE: {"title": [{"type": "text", "text": title_text}]},
            P_COMPANY: rt(listing.company),
            P_LOCATION: rt(listing.location),
            P_SKILLS: rt(listing.skills_cell()),
            P_JOB_ID: rt(listing.job_id),
            # Notion rejects multi_select values containing a comma.
            P_KEYWORDS: {"multi_select": [
                {"name": k.replace(",", " ")[:100]} for k in listing.resume_keywords[:25]
            ]},
            P_APPLIED: {"select": {"name": listing.applied or "Not applied"}},
            P_NOTES: rt(listing.notes),
        }
        if listing.category:
            props[P_CATEGORY] = {"select": {"name": _select(listing.category)}}
        # listing.term always resolves (falls back to "Unspecified" in config.term_for).
        props[P_TERM] = {"select": {"name": _select(listing.term)}}
        if listing.source:
            props[P_SOURCE] = {"select": {"name": _select(listing.source)}}
        if listing.apply_url:
            props[P_PORTAL] = {"url": listing.apply_url}
        if listing.posted_at:
            props[P_POSTED] = {"date": {"start": listing.posted_at.isoformat()}}
        if listing.recruiter:
            props[P_RECRUITER] = {"email": listing.recruiter}
        if listing.deadline:
            props[P_DEADLINE] = {"date": {"start": listing.deadline}}

        self._call("POST", "/pages", json={
            "parent": {"database_id": database_id},
            "properties": props,
        })

    def add_all(self, database_id: str, listings: Sequence) -> List:
        """Append listings one by one, surviving individual failures.

        Returns the listings that actually landed, not just how many: a
        caller announcing "new" listings must never include one whose write
        failed.
        """
        written = []
        for listing in listings:
            try:
                self.add(database_id, listing)
                written.append(listing)
            except Exception as exc:  # noqa: BLE001 - one bad row must not lose the rest
                log.error("failed to write %r (%s): %s", listing.role[:50], listing.job_id, exc)
            time.sleep(0.35)  # stay under Notion's ~3 req/s ceiling
        log.info("wrote %d/%d new listings", len(written), len(listings))
        return written
