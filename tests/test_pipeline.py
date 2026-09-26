import unittest
from unittest import mock

import ats
import internship_tracker
import parser as md


def make(job_url, **kw):
    base = dict(company="Riot Games", role="Gameplay Programmer Intern",
                location="LA", apply_url=job_url, source="studios",
                from_game_studio=True)
    base.update(kw)
    return md.Listing(**base)


class TestNotionRun(unittest.TestCase):
    def test_skips_listings_already_in_the_database(self):
        listings = [make("https://x.com/jobs/1111111"), make("https://x.com/jobs/2222222")]
        api = mock.Mock()
        api.existing_job_ids.return_value = {listings[0].job_id}
        api.add_all.return_value = 1
        internship_tracker.write_to_notion(api, "db1", listings, tombstoned=set())
        written = api.add_all.call_args[0][1]
        self.assertEqual([l.job_id for l in written], [listings[1].job_id])

    def test_skips_tombstoned_listings(self):
        listings = [make("https://x.com/jobs/1111111")]
        api = mock.Mock()
        api.existing_job_ids.return_value = set()
        internship_tracker.write_to_notion(api, "db1", listings,
                                           tombstoned={listings[0].job_id})
        self.assertEqual(api.add_all.call_args[0][1], [])

    def test_nothing_new_makes_no_write_call(self):
        listings = [make("https://x.com/jobs/1111111")]
        api = mock.Mock()
        api.existing_job_ids.return_value = {listings[0].job_id}
        internship_tracker.write_to_notion(api, "db1", listings, tombstoned=set())
        api.add_all.assert_not_called()


def backfill_row(needs_skills=True, needs_recruiter=True, url="https://x.com/job/1"):
    return {
        "page_id": "page-1",
        "url": url,
        "company": "Riot Games",
        "needs_skills": needs_skills,
        "needs_recruiter": needs_recruiter,
    }


class TestBackfill(unittest.TestCase):
    """backfill must never hit the network directly -- ats.fetch_page is mocked
    in every case here, so these tests exercise the recovery logic only.
    """

    def test_recovers_skills_and_recruiter_from_a_reachable_posting(self):
        api = mock.Mock()
        api.rows_to_backfill.return_value = [backfill_row()]
        page = ats.PageData(
            text="Requirements\n- Experience with Python programming\n",
            html="<html>Contact jobs@riotgames.com for more info</html>",
            ok=True,
        )
        with mock.patch("internship_tracker.ats.fetch_page", return_value=page):
            repaired = internship_tracker.backfill(api, "db1", limit=25)

        self.assertEqual(repaired, 1)
        api.rows_to_backfill.assert_called_once_with("db1", 25)
        api.update_row.assert_called_once_with(
            "page-1", skills="Experience with Python programming",
            recruiter="jobs@riotgames.com",
        )

    def test_leaves_row_untouched_when_the_page_is_unreachable(self):
        api = mock.Mock()
        api.rows_to_backfill.return_value = [backfill_row()]
        with mock.patch("internship_tracker.ats.fetch_page",
                        return_value=ats.PageData(ok=False)):
            repaired = internship_tracker.backfill(api, "db1")

        self.assertEqual(repaired, 0)
        api.update_row.assert_not_called()

    def test_only_recovers_the_field_flagged_as_missing(self):
        api = mock.Mock()
        api.rows_to_backfill.return_value = [
            backfill_row(needs_skills=True, needs_recruiter=False)
        ]
        page = ats.PageData(
            text="Requirements\n- Experience with Python programming\n",
            html="<html>Contact jobs@riotgames.com for more info</html>",
            ok=True,
        )
        with mock.patch("internship_tracker.ats.fetch_page", return_value=page):
            internship_tracker.backfill(api, "db1")

        api.update_row.assert_called_once_with(
            "page-1", skills="Experience with Python programming", recruiter="",
        )

    def test_leaves_row_untouched_when_nothing_recoverable_is_found(self):
        api = mock.Mock()
        api.rows_to_backfill.return_value = [backfill_row()]
        page = ats.PageData(text="No recognisable section here.", html="", ok=True)
        with mock.patch("internship_tracker.ats.fetch_page", return_value=page):
            repaired = internship_tracker.backfill(api, "db1")

        self.assertEqual(repaired, 0)
        api.update_row.assert_not_called()

    def test_default_limit_is_25(self):
        api = mock.Mock()
        api.rows_to_backfill.return_value = []
        internship_tracker.backfill(api, "db1")
        api.rows_to_backfill.assert_called_once_with("db1", 25)


if __name__ == "__main__":
    unittest.main()
