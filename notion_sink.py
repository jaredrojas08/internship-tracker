"""Notion database sink: schema creation, dedup, and row append.

Auth is an internal integration token (``NOTION_TOKEN``). Create one at
notion.so/my-integrations, then share the target page/database with it -- an
integration can only see what has been explicitly shared, which is the most
common reason this module 404s on a page that plainly exists.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Sequence, Set

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
P_NICHE = "Niche"
P_DEADLINE = "Deadline"

APPLIED_OPTIONS = [
    {"name": "Not applied", "color": "default"},
    {"name": "Applying", "color": "yellow"},
    {"name": "Applied", "color": "blue"},
    {"name": "Interviewing", "color": "purple"},
    {"name": "Offer", "color": "green"},
    {"name": "Rejected", "color": "red"},
]

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
    # Ticked when no mainstream aggregator carried this listing -- see
    # parser.Listing.is_niche. Filter the table on it for roles the big lists
    # never surfaced.
    P_NICHE: {"checkbox": {}},
    P_DEADLINE: {"date": {}},
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
        """One request with retry on Notion's 429 and transient 5xx."""
        for attempt in range(5):
            resp = self.s.request(method, f"{API}{path}", timeout=30, **kw)
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
        cursor: Optional[str] = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            page = self._call("POST", f"/databases/{database_id}/query", json=body)
            for row in page.get("results", []):
                props = row.get("properties", {})
                text = _plain_text(props.get(P_JOB_ID, {}).get("rich_text", []))
                if text:
                    ids.add(text)
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
        log.info("database already holds %d job ids", len(ids))
        return ids

    def add(self, database_id: str, listing) -> None:
        def rt(value: str) -> dict:
            return {"rich_text": [{"type": "text", "text": {"content": value[:2000]}}]} if value else {"rich_text": []}

        # The title links to the listing as the source published it. The
        # Application Portal column holds the resolved employer page, so the
        # row carries both: where it was found, and where to apply.
        title_text = {"content": listing.role[:2000]}
        origin = listing.apply_url or listing.portal_url
        if origin:
            title_text["link"] = {"url": origin}

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
            P_APPLIED: {"select": {"name": "Not applied"}},
            P_NOTES: rt(listing.notes),
        }
        if listing.category:
            props[P_CATEGORY] = {"select": {"name": _select(listing.category)}}
        if listing.source:
            props[P_SOURCE] = {"select": {"name": _select(listing.source)}}
        props[P_NICHE] = {"checkbox": listing.is_niche()}
        if listing.portal_url or listing.apply_url:
            props[P_PORTAL] = {"url": listing.portal_url or listing.apply_url}
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

    def add_all(self, database_id: str, listings: Sequence) -> int:
        """Append listings one by one, surviving individual failures."""
        written = 0
        for listing in listings:
            try:
                self.add(database_id, listing)
                written += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must not lose the rest
                log.error("failed to write %r (%s): %s", listing.role[:50], listing.job_id, exc)
            time.sleep(0.35)  # stay under Notion's ~3 req/s ceiling
        log.info("wrote %d/%d new listings", written, len(listings))
        return written
