"""Daily digest delivery.

The sheet is a pull interface: it is only useful when you remember to open it.
This turns the run into a push — a short message covering only what changed and
what needs acting on.

Channels are opt-in by secret. Set DISCORD_WEBHOOK_URL, or the SMTP_* vars for
email, or neither (in which case this silently does nothing and the run is
otherwise unaffected). A delivery failure never fails the run: the sheet is
already written by the time this is called.

DISCORD_NAG_WEBHOOK_URL sends the Applying reminder to its own channel. Unset,
the reminder goes to DISCORD_WEBHOOK_URL with everything else.
"""

import datetime as dt
import json
import logging
import os
import smtplib
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

import config
import parser as md_parser

log = logging.getLogger(__name__)

DIGEST_STATE_PATH = Path("digest_state.json")
DISCORD_LIMIT = 1900  # leave headroom under Discord's 2000-character cap
# A long digest is split across several Discord messages rather than cut off:
# a listing that is only visible on the sheet may as well not have been sent.
# The cap exists so a first run of 200+ listings can't post indefinitely — past
# this point the sheet really is the better tool.
MAX_DISCORD_MESSAGES = 6
DISCORD_SEND_PAUSE = 0.5  # seconds between messages; webhooks allow ~5/sec

# Discord rejects requests carrying urllib's default User-Agent with a bare
# 403, so identifying the client is required, not cosmetic.
USER_AGENT = "InternshipTracker (https://github.com/jaredrojas08/Internship-Tracker, 1.0)"


def is_applied(row):
    return row.get("Application") == "Applied"


def needs_follow_up(row, as_of=None):
    """True for an application submitted long enough ago to be worth chasing."""
    if not is_applied(row):
        return False
    applied = _date(row.get("Applied Date"))
    if applied is None:
        return False
    as_of = as_of or config.today()
    return (as_of - applied).days >= config.FOLLOW_UP_AFTER_DAYS


def awaiting_resume(rows):
    """Rows marked Applying with nothing attached yet: the tailoring queue."""
    return [r for r in rows
            if r.get("Application") == "Applying" and not r.get("Has Resume")]


def build_digest(rows, new_listings, dropped, as_of=None, warnings=(), totals=None,
                 board_problems=()):
    """Return (subject, body) summarising what needs attention, or None.

    Returns None when there is nothing actionable, so a quiet day sends no
    message rather than a daily "nothing happened" that trains you to ignore it.

    `totals`, when given, is (open_roles, total_applied) computed over the
    whole database rather than just `rows`. Pass it whenever `rows` is a
    filtered subset -- the Notion path always is -- since the summary line
    must never state a count `rows` cannot actually back up.
    """
    as_of = as_of or config.today()

    follow_ups = [r for r in rows if needs_follow_up(r, as_of)]
    soon = []
    for row in rows:
        due = _date(row.get("Deadline"))
        if due and 0 <= (due - as_of).days <= 14 and not is_applied(row):
            soon.append((due, row))
    soon.sort(key=lambda pair: pair[0])

    awaiting = awaiting_resume(rows)

    review = [l for l in new_listings if l.needs_review]
    new_listings = [l for l in new_listings if not l.needs_review]
    games = [l for l in new_listings if l.is_game]

    if not (new_listings or review or follow_ups or soon or warnings or awaiting
            or board_problems):
        if not config.NOTIFY_ON_QUIET_DAYS:
            return None
        return _quiet_digest(rows, as_of, totals=totals)

    parts = []

    # Source breakage goes first: everything below it is suspect when a source
    # is missing, so it must not be buried under the listings.
    if warnings:
        parts.append(_section("🚨 Source problem — the list may be incomplete", list(warnings)))

    if board_problems:
        parts.append(_section("⚠️ Studio boards to check: broken, moved, or nothing posted",
                              list(board_problems)))

    if review:
        parts.append(_section(
            f"🔎 Didn't match the filter, check these ({len(review)}, "
            "Category: Check: filtered out)",
            [_fmt_listing(l) for l in review]))

    if games:
        parts.append(_section("🎮 New game roles", [_fmt_listing(l) for l in games]))

    other_new = [l for l in new_listings if not l.is_game]
    if other_new:
        parts.append(
            _section(f"🆕 {len(other_new)} new listing(s)", [_fmt_listing(l) for l in other_new])
        )

    if soon:
        parts.append(
            _section(
                f"⏳ {len(soon)} known deadline(s) within 14 days",
                [f"{due.isoformat()} — {r['Company']} — {r['Role']}" for due, r in soon],
            )
        )

    if follow_ups:
        parts.append(
            _section(
                f"📮 {len(follow_ups)} application(s) with no reply after "
                f"{config.FOLLOW_UP_AFTER_DAYS} days",
                [
                    f"{r['Applied Date']} — {r['Company']} — {r['Role']}"
                    for r in follow_ups
                ],
            )
        )

    if awaiting:
        parts.append(
            _section(
                f"📝 {len(awaiting)} row(s) marked Applying with no resume attached",
                [f"{r['Company']} — {r['Role']}" for r in awaiting],
            )
        )

    if dropped:
        parts.append(f"🗑️ Removed {len(dropped)} listing(s) you unchecked.")

    if totals is not None:
        open_roles, total_applied = totals
    else:
        total_applied = sum(1 for r in rows if is_applied(r))
        open_roles = len(rows)
    parts.append(f"{open_roles} open · {total_applied} applied")

    # Games are counted separately in the headline, so pass only the remainder
    # to avoid announcing one game role as "1 game role, 1 new".
    headline = _headline(len(other_new), len(games), len(follow_ups), len(soon))
    if warnings:
        headline = "⚠️ Internship tracker: source problem"
    return headline, "\n\n".join(parts)


def _quiet_digest(rows, as_of, totals=None):
    """One line confirming the run happened and found nothing new.

    Deliberately terse and visually distinct from a real digest, so it can be
    dismissed at a glance — but present, so silence unambiguously means the run
    failed rather than that nothing happened.
    """
    if totals is not None:
        open_roles, applied = totals
    else:
        applied = sum(1 for r in rows if is_applied(r))
        open_roles = len(rows)

    upcoming = []
    for row in rows:
        due = _date(row.get("Deadline"))
        if due and due >= as_of and not is_applied(row):
            upcoming.append((due, row))
    upcoming.sort(key=lambda pair: pair[0])

    lines = [f"{open_roles} open · {applied} applied"]
    if upcoming:
        due, row = upcoming[0]
        days = (due - as_of).days
        # "known" is load-bearing: only a few percent of postings publish a
        # machine-readable deadline, so this is the soonest one on record, not
        # the soonest that exists. Stating the coverage keeps it honest.
        known = sum(1 for r in rows if _date(r.get("Deadline")))
        lines.append(
            f"📅 Next *known* deadline: {row['Company']} in {days}d "
            f"({due.isoformat()}) — only {known} of {len(rows)} listings publish one"
        )

    return (
        "Internship tracker: no new activity",
        "😴 **No new listings today.**\n" + "\n".join(lines),
    )


def _headline(new_count, game_count, follow_count, soon_count):
    bits = []
    if game_count:
        bits.append(f"{game_count} game role{'s' if game_count != 1 else ''}")
    if new_count:
        bits.append(f"{new_count} new")
    if soon_count:
        bits.append(f"{soon_count} deadline{'s' if soon_count != 1 else ''} soon")
    if follow_count:
        bits.append(f"{follow_count} to follow up")
    return "Internship tracker: " + (", ".join(bits) if bits else "update")


def _section(title, items):
    # Every item is listed. Length is handled once, at delivery, by splitting
    # across messages — truncating here would hide listings from email too,
    # which has no length limit at all.
    return f"**{title}**\n" + "\n".join(f"• {item}" for item in items)


def _fmt_listing(listing):
    tags = []
    if listing.is_remote:
        tags.append("REMOTE")
    if listing.salary:
        tags.append(listing.salary)
    suffix = f" [{', '.join(tags)}]" if tags else ""
    return f"{listing.company} — {listing.role}{suffix}"


def _date(value):
    try:
        return dt.date.fromisoformat(str(value).strip())
    except (ValueError, AttributeError):
        return None


# --- Cadence ----------------------------------------------------------------


# The fields _fmt_listing and the game/remote split actually read. Held in
# digest_state.json between runs so a listing found at 3am is still announced
# by the digest that fires at 6pm.
PENDING_FIELDS = ("company", "role", "location", "apply_url", "salary",
                  "from_game_studio", "needs_review")


def _read_state(path):
    """The digest state file as a dict. Anything unreadable reads as empty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path, last_sent, pending, last_nag=None):
    state = {"pending": pending}
    if last_sent:
        state["last_sent"] = last_sent
    if last_nag:
        state["last_nag"] = last_nag
    Path(path).write_text(json.dumps(state), encoding="utf-8")


def _is_due(last_sent, as_of, hours=24):
    if not isinstance(last_sent, str):
        return True
    try:
        sent_at = dt.datetime.fromisoformat(last_sent)
    except (ValueError, TypeError):
        return True
    if sent_at.tzinfo is None:
        # A naive timestamp could mean any timezone; nothing in this module
        # ever writes one, so treat it as unusable rather than guessing which
        # offset was meant.
        return True
    return as_of - sent_at >= dt.timedelta(hours=hours)


def digest_is_due(path, as_of=None):
    """True once a day has passed since the last digest, or none has ever sent.

    A missing or corrupt state file counts as due rather than raising: the
    scrape runs hourly now, so a swallowed error here would go silent for good
    instead of just sending one digest too many.
    """
    as_of = as_of or dt.datetime.now(dt.timezone.utc)
    return _is_due(_read_state(path).get("last_sent"), as_of)


def _merge_pending(stored, new_listings):
    """Add this run's finds to the ones earlier runs are still holding."""
    pending = [e for e in stored if isinstance(e, dict) and e.get("job_id")]
    have = {e["job_id"] for e in pending}
    for listing in new_listings:
        if listing.job_id in have:
            continue
        have.add(listing.job_id)
        entry = {"job_id": listing.job_id}
        entry.update({f: getattr(listing, f) for f in PENDING_FIELDS})
        pending.append(entry)
    return pending


def _pending_listings(pending):
    """Rebuild Listings from the stored fields, for _fmt_listing to format."""
    return [
        md_parser.Listing(
            company=entry.get("company", ""),
            role=entry.get("role", ""),
            location=entry.get("location", ""),
            apply_url=entry.get("apply_url", ""),
            salary=entry.get("salary", ""),
            from_game_studio=bool(entry.get("from_game_studio")),
            needs_review=bool(entry.get("needs_review")),
        )
        for entry in pending
    ]


def send_digest_if_due(rows, new_listings, dropped, warnings=(), state_path=DIGEST_STATE_PATH,
                        as_of=None, totals=None, board_problems=()):
    """Send one digest a day covering every listing found since the last one.

    Runs are hourly and the digest is daily, so this run's finds are parked in
    the state file and announced together when the gate opens. Dropping them
    instead would lose 23 of every 24 runs' listings.

    Source-failure warnings skip the gate and send right away, alone. They do
    not consume the day's slot and do not announce the parked listings: a 3am
    failure notice must not stand in for the real digest.
    """
    as_of = as_of or dt.datetime.now(dt.timezone.utc)
    state = _read_state(state_path)
    last_sent = state.get("last_sent")
    last_nag = state.get("last_nag")
    pending = _merge_pending(state.get("pending", []), new_listings)

    if not _is_due(last_sent, as_of):
        _write_state(state_path, last_sent, pending, last_nag)
        if warnings:
            digest = build_digest(rows, [], dropped, warnings=warnings, totals=totals)
            return bool(digest and send(*digest))

        # A row marked Applying is waiting on him, so it is chased between
        # digests rather than held for up to a day.
        awaiting = awaiting_resume(rows)
        if awaiting and _is_due(last_nag, as_of, config.NAG_INTERVAL_HOURS):
            body = _section(
                f"📝 {len(awaiting)} row(s) marked Applying with no resume attached",
                [f"{r['Company']} — {r['Role']}" for r in awaiting],
            )
            if send("Internship tracker: waiting on a resume", body,
                    webhook=os.environ.get("DISCORD_NAG_WEBHOOK_URL")):
                _write_state(state_path, last_sent, pending, as_of.isoformat())
                return True
            return False

        log.info("Digest already sent within the last day; holding %d new listing(s).",
                 len(pending))
        return False

    digest = build_digest(rows, _pending_listings(pending), dropped,
                          warnings=warnings, totals=totals, board_problems=board_problems)
    if not digest:
        _write_state(state_path, last_sent, pending, last_nag)
        return False
    if not send(*digest):
        # A failed or unconfigured send must not start the 24-hour cooldown or
        # discard the backlog: the next hourly run needs to retry.
        _write_state(state_path, last_sent, pending, last_nag)
        return False
    # The nag clock resets too, so the full digest naming a row is not repeated
    # by a nag two hours later.
    _write_state(state_path, as_of.isoformat(), [], as_of.isoformat())
    return True


# --- Delivery --------------------------------------------------------------


def send(subject, body, webhook=None):
    """Deliver to whichever channels are configured. Never raises.

    `webhook` overrides the Discord destination, which is how the Applying
    reminder reaches its own channel. Email has one address either way.
    """
    delivered, attempted = [], []

    discord_url = webhook or os.environ.get("DISCORD_WEBHOOK_URL")
    if discord_url:
        attempted.append("discord")
        if _send_discord(subject, body, discord_url):
            delivered.append("discord")
    if os.environ.get("SMTP_HOST") and os.environ.get("NOTIFY_EMAIL_TO"):
        attempted.append("email")
        if _send_email(subject, body):
            delivered.append("email")

    if delivered:
        log.info("Digest sent via %s", ", ".join(delivered))
    elif attempted:
        # Configured but every channel failed — distinct from not configured,
        # because the two need completely different fixes.
        log.warning("Digest delivery failed on: %s", ", ".join(attempted))
    else:
        log.info("No notification channel configured; skipping digest.")
    return delivered


def _chunk(text, limit=DISCORD_LIMIT):
    """Split text into <=limit pieces, breaking on line boundaries.

    Lines are kept whole so a listing is never cut in half across two messages.
    A single line longer than the limit (unusual — a very long role title) is
    hard-split, since there is nowhere else to break it.
    """
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _send_discord(subject, body, url):
    chunks = _chunk(f"**{subject}**\n\n{body}")
    dropped = len(chunks) - MAX_DISCORD_MESSAGES
    if dropped > 0:
        chunks = chunks[:MAX_DISCORD_MESSAGES]
        chunks[-1] += "\n… (too long for Discord — the rest is on the sheet)"

    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        if index > 1:
            # Continuation messages arrive without the header, so they need to
            # identify themselves as part of the same digest.
            chunk = f"_(continued {index}/{total})_\n{chunk}"
            time.sleep(DISCORD_SEND_PAUSE)
        if not _post_discord(chunk, url):
            # Stop rather than keep posting: a failure mid-digest already means
            # the message is incomplete, and the rest would read as noise.
            return index > 1
    return True


def _post_discord(message, url, retry=True):
    payload = json.dumps({"content": message}).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        urllib.request.urlopen(request, timeout=15)
        return True
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:200]
        except Exception:  # noqa: BLE001 - the error body is best-effort
            pass
        # Only reachable now that a digest can span several messages. Waiting
        # out the window is the whole fix: the alternative is a digest that
        # stops halfway through the listings.
        if exc.code == 429 and retry:
            wait = _retry_after(exc)
            log.info("Discord rate-limited the digest; retrying in %.1fs", wait)
            time.sleep(wait)
            return _post_discord(message, url, retry=False)
        if exc.code in (401, 403, 404):
            log.warning(
                "Discord rejected the webhook (HTTP %s). The URL is probably wrong, "
                "revoked, or from a deleted channel. %s",
                exc.code,
                detail,
            )
        else:
            log.warning("Discord delivery failed: HTTP %s %s", exc.code, detail)
        return False
    except (urllib.error.URLError, OSError) as exc:
        log.warning("Discord delivery failed: %s", exc)
        return False


def _retry_after(exc, default=2.0, ceiling=30.0):
    """Seconds to wait after a 429, clamped so a bad header can't stall the run."""
    try:
        wait = float(exc.headers.get("Retry-After", default))
    except (TypeError, ValueError, AttributeError):
        wait = default
    return max(0.0, min(wait, ceiling))


def _send_email(subject, body):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ.get("SMTP_USER", "internship-tracker")
    message["To"] = os.environ["NOTIFY_EMAIL_TO"]
    # Markdown bold reads fine as plain text, so it is left as-is.
    message.set_content(body)

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            if os.environ.get("SMTP_USER"):
                server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            server.send_message(message)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("Email delivery failed: %s", exc)
        return False


def send_test():
    """Send a sample digest so a new channel can be verified end to end.

    Uses obviously fake listings — a test message must never be mistakable for
    a real opening.
    """
    subject = "Internship tracker: test message"
    body = "\n\n".join(
        [
            "**✅ Digest delivery is working.**",
            _section(
                "🎮 New game roles",
                ["EXAMPLE STUDIO (not real) — Gameplay Engineer Intern [REMOTE]"],
            ),
            _section(
                f"📮 Applications with no reply after {config.FOLLOW_UP_AFTER_DAYS} days",
                ["2026-01-01 — EXAMPLE CORP (not real) — Software Engineer Intern"],
            ),
            "This is a test. Real digests only send when something actually changes.",
        ]
    )
    delivered = bool(send(subject, body))

    # A second webhook is a second channel with its own permissions, so it is
    # verified separately rather than assumed to work.
    nag_url = os.environ.get("DISCORD_NAG_WEBHOOK_URL")
    if nag_url:
        sent = _send_discord("Internship tracker: test message (Applying channel)",
                             "**✅ The Applying reminder lands here.**", nag_url)
        delivered = sent or delivered

    if not delivered:
        log.error(
            "No channel configured. Set DISCORD_WEBHOOK_URL, or the SMTP_* vars, "
            "then try again."
        )
    return delivered
