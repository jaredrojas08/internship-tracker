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

    def test_state_file_records_no_send_when_delivery_fails(self):
        with mock.patch("notify.send", return_value=[]):
            sent = notify.send_digest_if_due(
                [], [make_listing()], [], state_path=self.path, as_of=self.now
            )
        self.assertFalse(sent)
        # The cooldown must not start, but the listing must still be held so
        # the retry an hour from now still has something to announce.
        recorded = json.loads(self.path.read_text())
        self.assertNotIn("last_sent", recorded)
        self.assertEqual(len(recorded["pending"]), 1)


class TestDigestCoversEverythingSinceTheLastOne(unittest.TestCase):
    """Runs are hourly and the digest is daily, so a listing found in any of
    the other 23 runs has to survive in the state file until the gate opens.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "digest_state.json"
        self.now = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.dir.cleanup()

    def gated(self, hours_ago=1):
        self.path.write_text(json.dumps(
            {"last_sent": (self.now - timedelta(hours=hours_ago)).isoformat()}))

    def test_a_listing_found_while_gated_is_held_not_dropped(self):
        self.gated()
        with mock.patch("notify.send") as fake_send:
            notify.send_digest_if_due([], [make_listing()], [],
                                      state_path=self.path, as_of=self.now)
        fake_send.assert_not_called()
        self.assertEqual(len(json.loads(self.path.read_text())["pending"]), 1)

    def test_the_next_due_digest_announces_every_held_listing(self):
        self.gated()
        for i in range(3):
            notify.send_digest_if_due(
                [], [make_listing(role=f"Intern {i}",
                                  apply_url=f"https://x.com/jobs/{i}")],
                [], state_path=self.path, as_of=self.now + timedelta(hours=i),
            )

        with mock.patch("notify.send", return_value=["discord"]) as fake_send:
            sent = notify.send_digest_if_due([], [], [], state_path=self.path,
                                             as_of=self.now + timedelta(hours=25))
        self.assertTrue(sent)
        _subject, body = fake_send.call_args[0]
        for i in range(3):
            self.assertIn(f"Intern {i}", body)

    def test_a_sent_digest_clears_the_backlog(self):
        self.gated()
        notify.send_digest_if_due([], [make_listing()], [], state_path=self.path,
                                  as_of=self.now)
        with mock.patch("notify.send", return_value=["discord"]):
            notify.send_digest_if_due([], [], [], state_path=self.path,
                                      as_of=self.now + timedelta(hours=25))
        self.assertEqual(json.loads(self.path.read_text())["pending"], [])

    def test_the_same_listing_is_only_held_once(self):
        self.gated()
        for _ in range(3):
            notify.send_digest_if_due([], [make_listing()], [],
                                      state_path=self.path, as_of=self.now)
        self.assertEqual(len(json.loads(self.path.read_text())["pending"]), 1)

    def test_a_warning_bypass_does_not_consume_the_daily_slot(self):
        self.gated()
        notify.send_digest_if_due([], [make_listing()], [], state_path=self.path,
                                  as_of=self.now)
        with mock.patch("notify.send", return_value=["discord"]) as fake_send:
            notify.send_digest_if_due(
                [], [], [], warnings=["speedyapply: FAILED (timeout)"],
                state_path=self.path, as_of=self.now + timedelta(minutes=30),
            )
        # The alert went out, but it carried no listings and left both the
        # cooldown and the backlog alone.
        _subject, body = fake_send.call_args[0]
        self.assertIn("speedyapply", body)
        self.assertNotIn("Gameplay Programmer Intern", body)
        state = json.loads(self.path.read_text())
        self.assertEqual(state["last_sent"], (self.now - timedelta(hours=1)).isoformat())
        self.assertEqual(len(state["pending"]), 1)

    def test_the_real_digest_still_fires_after_a_warning_bypass(self):
        self.gated()
        notify.send_digest_if_due([], [make_listing()], [], state_path=self.path,
                                  as_of=self.now)
        with mock.patch("notify.send", return_value=["discord"]):
            notify.send_digest_if_due([], [], [], warnings=["speedyapply: FAILED"],
                                      state_path=self.path, as_of=self.now)
        with mock.patch("notify.send", return_value=["discord"]) as fake_send:
            sent = notify.send_digest_if_due([], [], [], state_path=self.path,
                                             as_of=self.now + timedelta(hours=25))
        self.assertTrue(sent)
        _subject, body = fake_send.call_args[0]
        self.assertIn("Gameplay Programmer Intern", body)

    def test_held_listings_keep_the_fields_the_digest_formats(self):
        self.gated()
        notify.send_digest_if_due(
            [], [make_listing(location="Remote", salary="$50/hr",
                              from_game_studio=True)],
            [], state_path=self.path, as_of=self.now,
        )
        with mock.patch("notify.send", return_value=["discord"]) as fake_send:
            notify.send_digest_if_due([], [], [], state_path=self.path,
                                      as_of=self.now + timedelta(hours=25))
        _subject, body = fake_send.call_args[0]
        self.assertIn("🎮 New game roles", body)
        self.assertIn("REMOTE", body)
        self.assertIn("$50/hr", body)


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


class TestApplyingNag(unittest.TestCase):
    """Rows marked Applying with no resume attached are the tailoring queue."""

    def _rows(self, **kw):
        base = {"Company": "Epic Games", "Role": "Gameplay Programmer Intern",
                "Deadline": "", "Application": "Applying", "Applied Date": "",
                "Has Resume": False}
        base.update(kw)
        return [base]

    def test_applying_without_a_resume_is_nagged(self):
        subject, body = notify.build_digest(self._rows(), [], [], totals=(745, 0))
        self.assertIn("Applying", body)
        self.assertIn("Epic Games", body)

    def test_applying_with_a_resume_attached_is_not_nagged(self):
        _, body = notify.build_digest(self._rows(**{"Has Resume": True}), [], [], totals=(745, 0))
        self.assertNotIn("no resume attached", body)

    def test_a_row_not_marked_applying_is_not_nagged(self):
        _, body = notify.build_digest(self._rows(Application="Not applied"), [], [], totals=(745, 0))
        self.assertNotIn("no resume attached", body)

    def test_the_nag_alone_is_enough_to_send_a_digest(self):
        # A day with no new listings still sends if something is waiting on him.
        self.assertIsNotNone(notify.build_digest(self._rows(), [], [], totals=(745, 0)))


class TestNagCadence(unittest.TestCase):
    """The Applying nag runs on its own 2-hour clock, separate from the digest."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "digest_state.json"
        self.now = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.dir.cleanup()

    def _state(self, **kw):
        self.path.write_text(json.dumps(kw))

    def _rows(self, has_resume=False):
        return [{"Company": "Epic Games", "Role": "Gameplay Programmer Intern",
                 "Deadline": "", "Application": "Applying", "Applied Date": "",
                 "Has Resume": has_resume}]

    def _send(self, rows, **kw):
        with mock.patch.object(notify, "send", return_value=["discord"]) as sender:
            sent = notify.send_digest_if_due(rows, [], [], state_path=self.path,
                                             as_of=self.now, totals=(743, 0), **kw)
        return sent, sender

    def test_nag_fires_between_daily_digests(self):
        # Digest went an hour ago, so the daily gate is shut, but a row is waiting.
        self._state(last_sent=(self.now - timedelta(hours=1)).isoformat(), pending=[])
        sent, sender = self._send(self._rows())
        self.assertTrue(sent)
        self.assertIn("no resume attached", sender.call_args[0][1])

    def test_nag_holds_for_two_hours_after_nagging(self):
        self._state(last_sent=(self.now - timedelta(hours=1)).isoformat(),
                    last_nag=(self.now - timedelta(minutes=30)).isoformat(), pending=[])
        sent, sender = self._send(self._rows())
        self.assertFalse(sent)
        sender.assert_not_called()

    def test_nag_fires_again_after_two_hours(self):
        self._state(last_sent=(self.now - timedelta(hours=1)).isoformat(),
                    last_nag=(self.now - timedelta(hours=3)).isoformat(), pending=[])
        sent, _ = self._send(self._rows())
        self.assertTrue(sent)

    def test_no_nag_when_nothing_is_waiting(self):
        self._state(last_sent=(self.now - timedelta(hours=1)).isoformat(), pending=[])
        sent, sender = self._send(self._rows(has_resume=True))
        self.assertFalse(sent)
        sender.assert_not_called()

    def test_nagging_does_not_consume_the_daily_slot(self):
        self._state(last_sent=(self.now - timedelta(hours=1)).isoformat(), pending=[])
        self._send(self._rows())
        self.assertEqual(json.loads(self.path.read_text())["last_sent"],
                         (self.now - timedelta(hours=1)).isoformat())

    def test_the_daily_digest_also_resets_the_nag_clock(self):
        # Otherwise the full digest names the row, then a nag repeats it 2h later.
        self._state(last_sent=(self.now - timedelta(hours=25)).isoformat(), pending=[])
        self._send(self._rows())
        self.assertEqual(json.loads(self.path.read_text())["last_nag"], self.now.isoformat())
