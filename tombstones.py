"""Job IDs the user deleted from Notion, so the next run does not re-add them.

The Sheet had a hidden Removed tab for this. Notion has no equivalent, since a
deleted page stops coming back from the API, which is exactly what makes it
look new again.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("removed.json")


def load(path=DEFAULT_PATH):
    """Every tombstoned job id. A missing or unreadable file means none."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return set()
        return {item for item in data if isinstance(item, str)}
    except Exception:
        return set()


def add(job_ids, path=DEFAULT_PATH):
    """Tombstone these ids. Returns how many were new."""
    current = load(path)
    incoming = {j for j in job_ids if j}
    new = incoming - current
    if new:
        Path(path).write_text(json.dumps(sorted(current | incoming), indent=1),
                              encoding="utf-8")
        log.info("tombstoned %d job id(s)", len(new))
    return len(new)
