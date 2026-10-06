import unittest
from datetime import date
from unittest import mock

import ats
import enrich
import internship_tracker
import parser as md


def make(job_url, **kw):
    base = dict(company="Riot Games", role="Gameplay Programmer Intern",
                location="LA", apply_url=job_url, source="studios",
                from_game_studio=True)
    base.update(kw)
    return md.Listing(**base)


class TestSelectNew(unittest.TestCase):
    def test_skips_listings_already_in_the_database(self):
        listings = [make("https://x.com/jobs/1111111"), make("https://x.com/jobs/2222222")]
        fresh = internship_tracker.select_new(listings, {listings[0].job_id}, set())
        self.assertEqual([l.job_id for l in fresh], [listings[1].job_id])

    def test_skips_tombstoned_listings(self):
        listings = [make("https://x.com/jobs/1111111")]
        self.assertEqual(
            internship_tracker.select_new(listings, set(), {listings[0].job_id}), []
        )

    def test_everything_is_new_against_an_empty_database(self):
        listings = [make("https://x.com/jobs/1111111"), make("https://x.com/jobs/2222222")]
        self.assertEqual(internship_tracker.select_new(listings, set(), set()), listings)


PROGRAMS_MARKDOWN = """
## programs open now

| Organization | Opportunity | Type | Deadline |
|---|---|---|---|
| MLT | [Career Prep](https://mlt.org/apply) | Fellowship | Rolling |
"""


class TestMainRun(unittest.TestCase):
    """A full --notion run against fakes. Nothing here touches the network.

    These pin the two things the enrich-before-write reorder made fragile: a
    program row must keep its own category, and a posting Notion already holds
    must never be fetched again.
    """

    def run_main(self, listings, existing, argv=("--notion", "--no-notify")):
        api = mock.Mock()
        api.existing_job_ids.return_value = existing
        api.existing_title_fingerprints.return_value = {}
        api.add_all.side_effect = lambda db, batch: list(batch)
        api.rows_to_backfill.return_value = []
        api.rows_missing_applied_date.return_value = []
        api.rows_for_digest.return_value = ([], 0, 0)

        enriched = []

        def fake_enrich_all(batch, **kw):
            # Stand-in for the real thing, which fetches every posting page
            # and overwrites category from is_game.
            enriched.extend(batch)
            for listing in batch:
                listing.category = enrich.category_for(listing)

        with mock.patch("internship_tracker.notion_sink.Notion", return_value=api), \
             mock.patch("internship_tracker.config.require_env", return_value="x"), \
             mock.patch("internship_tracker.sources.fetch_all", return_value=listings), \
             mock.patch("internship_tracker.md_parser.fetch_readme",
                        return_value=PROGRAMS_MARKDOWN), \
             mock.patch("internship_tracker.tombstones.load", return_value=set()), \
             mock.patch("internship_tracker.enrich.enrich_all", fake_enrich_all):
            exit_code = internship_tracker.main(list(argv))

        written = api.add_all.call_args[0][1] if api.add_all.called else []
        return exit_code, enriched, written

    def test_programs_keep_their_category_through_a_full_run(self):
        exit_code, _enriched, written = self.run_main([make("https://x.com/jobs/1")], set())
        self.assertEqual(exit_code, 0)
        programs = [l for l in written if l.source == "programs"]
        self.assertEqual([l.category for l in programs], ["Program / Fellowship"])

    def test_enrichment_only_runs_for_listings_not_already_in_notion(self):
        known = make("https://x.com/jobs/1111111", role="Gameplay Intern")
        unknown = make("https://x.com/jobs/2222222", role="Engine Intern")
        _code, enriched, _written = self.run_main([known, unknown], {known.job_id})
        self.assertEqual([l.job_id for l in enriched], [unknown.job_id])

    def test_a_run_with_nothing_new_enriches_nothing(self):
        known = make("https://x.com/jobs/1111111")
        _code, enriched, _written = self.run_main([known], {known.job_id})
        self.assertEqual(enriched, [])

    def test_programs_are_never_enriched(self):
        _code, enriched, written = self.run_main([make("https://x.com/jobs/1")], set())
        self.assertTrue(any(l.source == "programs" for l in written))
        self.assertFalse(any(l.source == "programs" for l in enriched))

    def test_every_written_listing_carries_a_posting_date(self):
        # first_seen is the last rung of the ladder; a blank Posted blanks the
        # Hours Since Posted column the Recent view sorts on.
        _code, _enriched, written = self.run_main([make("https://x.com/jobs/1")], set())
        self.assertTrue(written)
        for listing in written:
            self.assertIsNotNone(listing.posted_at)
            self.assertEqual(listing.posted_precision, "first_seen")


class TestNotionFailureAlerts(unittest.TestCase):
    def test_a_notion_outage_alerts_discord_and_exits_non_zero(self):
        api = mock.Mock()
        api.ensure_schema.side_effect = RuntimeError("Notion GET /databases/db1 -> 503")
        with mock.patch("internship_tracker.notion_sink.Notion", return_value=api), \
             mock.patch("internship_tracker.config.require_env", return_value="x"), \
             mock.patch("internship_tracker.sources.fetch_all",
                        return_value=[make("https://x.com/jobs/1")]), \
             mock.patch("internship_tracker.md_parser.fetch_readme", return_value=""), \
             mock.patch("internship_tracker.notify.send",
                        return_value=["discord"]) as fake_send:
            exit_code = internship_tracker.main(["--notion", "--no-notify"])

        self.assertEqual(exit_code, 1)
        fake_send.assert_called_once()
        self.assertIn("503", fake_send.call_args[0][1])

    def test_nothing_is_written_when_the_dedup_read_fails(self):
        api = mock.Mock()
        api.existing_job_ids.side_effect = RuntimeError("no Job ID column")
        with mock.patch("internship_tracker.notion_sink.Notion", return_value=api), \
             mock.patch("internship_tracker.config.require_env", return_value="x"), \
             mock.patch("internship_tracker.sources.fetch_all",
                        return_value=[make("https://x.com/jobs/1")]), \
             mock.patch("internship_tracker.md_parser.fetch_readme", return_value=""), \
             mock.patch("internship_tracker.notify.send", return_value=["discord"]):
            exit_code = internship_tracker.main(["--notion", "--no-notify"])

        self.assertEqual(exit_code, 1)
        api.add_all.assert_not_called()


class TestTombstoneFlag(unittest.TestCase):
    """The production caller for tombstones. Without it a page deleted in
    Notion comes straight back on the next run.
    """

    def test_the_flag_records_the_ids_and_fetches_nothing(self):
        with mock.patch("internship_tracker.tombstones.add", return_value=2) as add, \
             mock.patch("internship_tracker.tombstones.load", return_value=set()), \
             mock.patch("internship_tracker.sources.fetch_all") as fetch_all:
            exit_code = internship_tracker.main(
                ["--tombstone", "greenhouse.io:123", "lever.co:456"]
            )
        self.assertEqual(exit_code, 0)
        add.assert_called_once_with(["greenhouse.io:123", "lever.co:456"])
        fetch_all.assert_not_called()

    def test_a_tombstoned_id_is_never_written_on_the_next_run(self):
        listing = make("https://x.com/jobs/1111111")
        with mock.patch("internship_tracker.tombstones.load",
                        return_value={listing.job_id}):
            self.assertEqual(
                internship_tracker.select_new([listing], set(),
                                              internship_tracker.tombstones.load()),
                [],
            )


def backfill_row(needs_skills=True, needs_recruiter=True, url="https://x.com/job/1",
                  page_id="page-1"):
    return {
        "page_id": page_id,
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
        sent = api.update_row.call_args.kwargs
        self.assertEqual(sent["skills"], "Experience with Python programming")
        self.assertEqual(sent["recruiter"], "jobs@riotgames.com")

    def test_marks_an_unreachable_page_so_it_leaves_the_queue(self):
        api = mock.Mock()
        api.rows_to_backfill.return_value = [backfill_row()]
        with mock.patch("internship_tracker.ats.fetch_page",
                        return_value=ats.PageData(ok=False)):
            repaired = internship_tracker.backfill(api, "db1")

        self.assertEqual(repaired, 0)
        # Marked, not skipped: the row must stop coming back every hour.
        api.update_row.assert_called_once_with("page-1", skills="See posting")

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
            "page-1", skills="Experience with Python programming",
        )

    def test_marks_a_page_with_no_requirements_so_it_leaves_the_queue(self):
        api = mock.Mock()
        api.rows_to_backfill.return_value = [backfill_row()]
        page = ats.PageData(text="No recognisable section here.", html="", ok=True)
        with mock.patch("internship_tracker.ats.fetch_page", return_value=page):
            repaired = internship_tracker.backfill(api, "db1")

        self.assertEqual(repaired, 1)
        sent = api.update_row.call_args.kwargs
        self.assertEqual(sent["skills"], "See posting")

    def test_default_limit_is_25(self):
        api = mock.Mock()
        api.rows_to_backfill.return_value = []
        internship_tracker.backfill(api, "db1")
        api.rows_to_backfill.assert_called_once_with("db1", 25)

    def test_one_failing_row_does_not_abandon_the_rest_of_the_batch(self):
        api = mock.Mock()
        rows = [
            backfill_row(page_id="page-1"),
            backfill_row(page_id="page-2"),
            backfill_row(page_id="page-3"),
        ]
        api.rows_to_backfill.return_value = rows

        def update_row(page_id, skills="", recruiter=""):
            if page_id == "page-2":
                raise RuntimeError("Notion PATCH /pages/page-2 -> 500")
        api.update_row.side_effect = update_row

        page = ats.PageData(
            text="Requirements\n- Experience with Python programming\n",
            html="<html>Contact jobs@riotgames.com for more info</html>",
            ok=True,
        )
        with mock.patch("internship_tracker.ats.fetch_page", return_value=page):
            repaired = internship_tracker.backfill(api, "db1")

        self.assertEqual(repaired, 2)
        attempted = [call.args[0] for call in api.update_row.call_args_list]
        self.assertEqual(attempted, ["page-1", "page-2", "page-3"])


class TestStampAppliedDates(unittest.TestCase):
    def test_stamps_a_row_seen_applied_for_the_first_time(self):
        api = mock.Mock()
        api.rows_missing_applied_date.return_value = [{"page_id": "page-1"}]
        as_of = date(2026, 9, 26)

        stamped = internship_tracker.stamp_applied_dates(api, "db1", as_of=as_of)

        self.assertEqual(stamped, 1)
        api.stamp_applied_date.assert_called_once_with("page-1", "2026-09-26")

    def test_a_row_with_nothing_to_stamp_is_left_alone_on_a_later_run(self):
        # rows_missing_applied_date is the source of truth for "already stamped";
        # once it stops returning a row, this function must not touch it again.
        api = mock.Mock()
        api.rows_missing_applied_date.return_value = []

        stamped = internship_tracker.stamp_applied_dates(api, "db1", as_of=date(2026, 9, 27))

        self.assertEqual(stamped, 0)
        api.stamp_applied_date.assert_not_called()


class TestCreateDatabase(unittest.TestCase):
    def test_create_database_never_fetches_sources(self):
        fake_api = mock.Mock()
        fake_api.create_database.return_value = "db-123"
        with mock.patch("internship_tracker.notion_sink.Notion", return_value=fake_api), \
             mock.patch("internship_tracker.config.require_env", return_value="x"), \
             mock.patch("internship_tracker.sources.fetch_all") as fetch_all:
            exit_code = internship_tracker.main(["--create-database"])

        self.assertEqual(exit_code, 0)
        fetch_all.assert_not_called()
        fake_api.create_database.assert_called_once()


if __name__ == "__main__":
    unittest.main()


JOBRIGHT_MARKDOWN = """
| Company | Job Title | Location | Work Model | Date Posted |
| ----- | --------- |  --------- | ---- | ------- |
| **[Ramp](https://ramp.com)** | **[Software Engineering Intern, Backend](https://jobright.ai/jobs/info/6ac4aaa?utm_campaign=1079&utm_source=git)** | New York, NY, United States | On Site | Oct 05 |
| **[Ludia](http://www.ludia.com)** | **[Game Programming Intern](https://jobright.ai/jobs/info/6ac3bbb?utm_campaign=1079)** | Montréal, QC, Canada | Hybrid | Oct 05 |
| **[Acme](https://acme.com)** | **[Cloud Engineer Intern](https://jobright.ai/jobs/info/6ac3ccc)** | Austin, TX, United States | Remote | Oct 05 |
"""


class TestJobright(unittest.TestCase):
    def test_parses_us_rows_and_applies_the_filters(self):
        import sources
        listings = sources.parse_jobright(JOBRIGHT_MARKDOWN)
        self.assertEqual([(l.company, l.role, l.source) for l in listings],
                         [("Ramp", "Software Engineering Intern, Backend", "jobright")])
        self.assertEqual(listings[0].location, "New York, NY, United States")
        self.assertTrue(listings[0].apply_url.startswith("https://jobright.ai/jobs/info/6ac4aaa"))

    def test_a_jobright_copy_of_an_existing_row_is_skipped(self):
        listing = make("https://jobright.ai/jobs/info/abc", source="jobright")
        existing_titles = {sources_fp(listing): "speedyapply"}
        self.assertEqual(internship_tracker.select_new([listing], set(), set(), existing_titles), [])

    def test_the_real_posting_is_skipped_after_jobright_added_it(self):
        listing = make("https://boards.greenhouse.io/x/jobs/1234567", source="speedyapply")
        existing_titles = {sources_fp(listing): "jobright"}
        self.assertEqual(internship_tracker.select_new([listing], set(), set(), existing_titles), [])

    def test_a_longer_company_name_still_matches(self):
        # jobright writes "Electronic Arts (EA)" where the studio board says "Electronic Arts".
        listing = make("https://jobright.ai/jobs/info/abc", source="jobright",
                       company="Electronic Arts (EA)", role="Software Engineer Intern - SUMMER 2027")
        existing_titles = {("electronicarts", "softwareengineerinternsummer2027"): "studios"}
        self.assertEqual(internship_tracker.select_new([listing], set(), set(), existing_titles), [])

    def test_same_title_at_a_different_company_is_kept(self):
        listing = make("https://jobright.ai/jobs/info/abc", source="jobright",
                       company="Docusign", role="Software Engineer Intern")
        existing_titles = {("autoowners", "softwareengineerintern"): "speedyapply"}
        self.assertEqual(internship_tracker.select_new([listing], set(), set(), existing_titles), [listing])

    def test_same_title_from_two_real_sources_is_still_kept(self):
        # Raytheon posts the same title per city; only jobright rows match on title.
        listing = make("https://x.com/jobs/7654321", source="speedyapply")
        existing_titles = {sources_fp(listing): "speedyapply"}
        self.assertEqual(internship_tracker.select_new([listing], set(), set(), existing_titles), [listing])


def sources_fp(listing):
    import sources
    return sources.title_fingerprint(listing.company, listing.role)
