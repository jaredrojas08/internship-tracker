import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import notify
import parser as md


def make_listing(**kw):
    base = dict(company="Riot Games", role="Gameplay Programmer Intern",
                location="LA", apply_url="https://x.com/jobs/1", source="studios")
    base.update(kw)
    return md.Listing(**base)


class TestDigestCadence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "digest_state.json"
        self.now = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.dir.cleanup()

    def test_first_ever_run_is_due(self):
        self.assertTrue(notify.digest_is_due(self.path, self.now))

    def test_not_due_an_hour_after_sending(self):
        self.path.write_text(json.dumps({"last_sent": (self.now - timedelta(hours=1)).isoformat()}))
        self.assertFalse(notify.digest_is_due(self.path, self.now))

    def test_due_again_after_24_hours(self):
        self.path.write_text(json.dumps({"last_sent": (self.now - timedelta(hours=25)).isoformat()}))
        self.assertTrue(notify.digest_is_due(self.path, self.now))

    def test_corrupt_state_is_treated_as_due(self):
        self.path.write_text("{not json")
        self.assertTrue(notify.digest_is_due(self.path, self.now))

    def test_naive_last_sent_is_treated_as_due(self):
        # No tzinfo at all -- comparing it against an aware "as_of" used to
        # raise TypeError instead of degrading to "due".
        self.path.write_text(json.dumps({"last_sent": "2026-09-24T17:59:00"}))
        self.assertTrue(notify.digest_is_due(self.path, self.now))

    def test_aware_last_sent_compares_correctly_across_offsets(self):
        # Same instant as one hour before self.now, just expressed five hours
        # ahead of UTC, to prove the comparison converts rather than string-matches.
        five_hours_east = timezone(timedelta(hours=5))
        sent_at = (self.now - timedelta(hours=1)).astimezone(five_hours_east)
        self.path.write_text(json.dumps({"last_sent": sent_at.isoformat()}))
        self.assertFalse(notify.digest_is_due(self.path, self.now))


class TestSendDigestIfDue(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "digest_state.json"
        self.now = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.dir.cleanup()

    def test_sends_when_due(self):
        with mock.patch("notify.send", return_value=["discord"]) as fake_send:
            sent = notify.send_digest_if_due(
                [], [make_listing()], [], state_path=self.path, as_of=self.now
            )
        self.assertTrue(sent)
        fake_send.assert_called_once()

    def test_does_not_send_when_not_due(self):
        self.path.write_text(json.dumps({"last_sent": (self.now - timedelta(hours=1)).isoformat()}))
        with mock.patch("notify.send") as fake_send:
            sent = notify.send_digest_if_due(
                [], [make_listing()], [], state_path=self.path, as_of=self.now
            )
        self.assertFalse(sent)
        fake_send.assert_not_called()

    def test_warnings_send_regardless_of_the_daily_gate(self):
        self.path.write_text(json.dumps({"last_sent": (self.now - timedelta(hours=1)).isoformat()}))
        with mock.patch("notify.send", return_value=["discord"]) as fake_send:
            sent = notify.send_digest_if_due(
                [], [], [], warnings=["speedyapply: FAILED (timeout)"],
                state_path=self.path, as_of=self.now,
            )
        self.assertTrue(sent)
        fake_send.assert_called_once()

    def test_state_file_is_updated_after_a_successful_send(self):
        with mock.patch("notify.send", return_value=["discord"]):
            notify.send_digest_if_due(
                [], [make_listing()], [], state_path=self.path, as_of=self.now
            )
        recorded = json.loads(self.path.read_text())
        self.assertEqual(recorded["last_sent"], self.now.isoformat())

    def test_state_file_is_not_updated_when_delivery_fails(self):
        with mock.patch("notify.send", return_value=[]):
            sent = notify.send_digest_if_due(
                [], [make_listing()], [], state_path=self.path, as_of=self.now
            )
        self.assertFalse(sent)
        self.assertFalse(self.path.exists())


class TestFollowUpFromNotionShapedRows(unittest.TestCase):
    """Notion's rows_for_digest never sets Deadline for an applied-only row,
    so these prove the follow-up section works from that shape directly.
    """

    def notion_row(self, applied_date, deadline=""):
        return {
            "Company": "Riot Games",
            "Role": "Gameplay Intern",
            "Deadline": deadline,
            "Application": "Applied",
            "Applied Date": applied_date,
        }

    def test_needs_follow_up_fires_with_no_deadline_on_the_row(self):
        row = self.notion_row(applied_date="2026-08-01")
        self.assertTrue(notify.needs_follow_up(row, date(2026, 9, 26)))

    def test_build_digest_surfaces_the_follow_up_from_a_notion_row(self):
        row = self.notion_row(applied_date="2026-08-01")
        digest = notify.build_digest([row], [], [], as_of=date(2026, 9, 26))
        self.assertIsNotNone(digest)
        _subject, body = digest
        self.assertIn("no reply after", body)
        self.assertIn("Riot Games", body)


class TestSummaryLineTotals(unittest.TestCase):
    def test_summary_line_uses_totals_not_the_filtered_subset_size(self):
        # `rows` here is a stand-in for rows_for_digest's filtered slice: one
        # row, but the real database (per `totals`) holds far more. The line
        # must report the database's numbers, never len(rows).
        row = {
            "Company": "Riot Games",
            "Role": "Gameplay Intern",
            "Deadline": "2026-10-01",
            "Application": "Not applied",
        }
        _subject, body = notify.build_digest(
            [row], [], [], as_of=date(2026, 9, 26), totals=(745, 62)
        )
        self.assertIn("745 open · 62 applied", body)


if __name__ == "__main__":
    unittest.main()
