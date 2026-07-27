"""Daily digest delivery.

The sheet is a pull interface: it is only useful when you remember to open it.
This turns the run into a push — a short message covering only what changed and
what needs acting on.

Channels are opt-in by secret. Set DISCORD_WEBHOOK_URL, or the SMTP_* vars for
email, or neither (in which case this silently does nothing and the run is
otherwise unaffected). A delivery failure never fails the run: the sheet is
already written by the time this is called.
"""

import datetime as dt
import json
import logging
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

import config
import sheets

log = logging.getLogger(__name__)

DISCORD_LIMIT = 1900  # leave headroom under Discord's 2000-character cap
MAX_ITEMS = 10  # per section, so one busy day can't produce a wall of text


def build_digest(rows, new_listings, dropped, as_of=None):
    """Return (subject, body) summarising what needs attention, or None.

    Returns None when there is nothing actionable, so a quiet day sends no
    message rather than a daily "nothing happened" that trains you to ignore it.
    """
    as_of = as_of or sheets.today()

    follow_ups = [r for r in rows if sheets.needs_follow_up(r, as_of)]
    dead_applied = [
        r
        for r in rows
        if r.get("Applied?") and r.get("Link Status") in ("DEAD", "CLOSED")
    ]
    soon = []
    for row in rows:
        due = _date(row.get("Deadline"))
        if due and 0 <= (due - as_of).days <= 14 and not row.get("Applied?"):
            soon.append((due, row))
    soon.sort(key=lambda pair: pair[0])

    games = [l for l in new_listings if l.is_game]

    if not (new_listings or follow_ups or dead_applied or soon):
        return None

    parts = []

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
                f"⏳ {len(soon)} deadline(s) within 14 days",
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

    if dead_applied:
        parts.append(
            _section(
                f"⚠️ {len(dead_applied)} role(s) you applied to are now closed",
                [f"{r['Company']} — {r['Role']}" for r in dead_applied],
            )
        )

    if dropped:
        parts.append(f"🗑️ Removed {len(dropped)} listing(s) you unchecked.")

    total_applied = sum(1 for r in rows if r.get("Applied?"))
    parts.append(f"\n{len(rows)} listings tracked · {total_applied} applied")

    headline = _headline(len(new_listings), len(games), len(follow_ups), len(soon))
    return headline, "\n\n".join(parts)


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
    shown = items[:MAX_ITEMS]
    text = f"**{title}**\n" + "\n".join(f"• {item}" for item in shown)
    if len(items) > MAX_ITEMS:
        text += f"\n• …and {len(items) - MAX_ITEMS} more"
    return text


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


# --- Delivery --------------------------------------------------------------


def send(subject, body):
    """Deliver to whichever channels are configured. Never raises."""
    delivered = []
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        if _send_discord(subject, body):
            delivered.append("discord")
    if os.environ.get("SMTP_HOST") and os.environ.get("NOTIFY_EMAIL_TO"):
        if _send_email(subject, body):
            delivered.append("email")

    if delivered:
        log.info("Digest sent via %s", ", ".join(delivered))
    else:
        log.info("No notification channel configured; skipping digest.")
    return delivered


def _send_discord(subject, body):
    message = f"**{subject}**\n\n{body}"
    if len(message) > DISCORD_LIMIT:
        message = message[:DISCORD_LIMIT] + "\n… (truncated, see the sheet)"
    payload = json.dumps({"content": message}).encode()
    request = urllib.request.Request(
        os.environ["DISCORD_WEBHOOK_URL"],
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=15)
        return True
    except (urllib.error.URLError, OSError) as exc:
        log.warning("Discord delivery failed: %s", exc)
        return False


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
