import unittest

import notion_sink
import parser as md


class FakeNotion(notion_sink.Notion):
    """Records every call instead of making it."""

    def __init__(self):
        super().__init__("ntn_fake")
        self.calls = []
        self.responses = []

    def _call(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        return self.responses.pop(0) if self.responses else {"id": "fake-id", "results": []}


def make(**kw):
    base = dict(company="Riot Games", role="Gameplay Programmer Intern",
                location="Los Angeles, CA",
                apply_url="https://boards.greenhouse.io/riotgames/jobs/1234567",
                source="studios", from_game_studio=True)
    base.update(kw)
    return md.Listing(**base)


class TestSchema(unittest.TestCase):
    def test_schema_has_every_documented_property(self):
        expected = {"Title", "Company", "Category", "Term", "Location",
                    "Application Portal", "Resume Keywords", "Skill Requirements",
                    "Posted", "Hours Since Posted", "Recruiter Contact", "Applied",
                    "My Resume PDF", "Source", "Job ID", "Notes", "Deadline",
                    "Applied Date"}
        self.assertEqual(set(notion_sink.SCHEMA), expected)

    def test_applied_options_match_the_agreed_labels_and_colours(self):
        self.assertEqual(
            [(o["name"], o["color"]) for o in notion_sink.APPLIED_OPTIONS],
            [("Not applied", "default"), ("Applying", "yellow"), ("Applied", "blue"),
             ("Interviewing", "purple"), ("Offer", "green"), ("Rejected", "red"),
             ("Skipped", "gray")],
        )

    def test_deadline_is_a_date_not_text(self):
        self.assertIn("date", notion_sink.SCHEMA["Deadline"])

    def test_applied_date_is_a_date_not_text(self):
        self.assertIn("date", notion_sink.SCHEMA["Applied Date"])

    def test_hours_since_posted_is_a_formula_over_posted(self):
        formula = notion_sink.SCHEMA["Hours Since Posted"]["formula"]["expression"]
        self.assertIn('prop("Posted")', formula)
        self.assertIn("hours", formula)


class TestAdd(unittest.TestCase):
    def test_writes_one_page_with_the_job_id(self):
        api = FakeNotion()
        listing = make()
        api.add("db1", listing)
        method, path, body = api.calls[-1]
        self.assertEqual((method, path), ("POST", "/pages"))
        job_id = body["properties"]["Job ID"]["rich_text"][0]["text"]["content"]
        self.assertEqual(job_id, listing.job_id)

    def test_new_row_defaults_to_not_applied(self):
        api = FakeNotion()
        api.add("db1", make())
        body = api.calls[-1][2]
        self.assertEqual(body["properties"]["Applied"]["select"]["name"], "Not applied")

    def test_title_links_to_the_posting(self):
        api = FakeNotion()
        listing = make()
        api.add("db1", listing)
        title = api.calls[-1][2]["properties"]["Title"]["title"][0]["text"]
        self.assertEqual(title["link"]["url"], listing.apply_url)

    def test_select_values_never_contain_a_comma(self):
        # Notion rejects a select option with a comma and fails the whole write.
        api = FakeNotion()
        api.add("db1", make(source="studios,speedyapply"))
        self.assertNotIn(",", api.calls[-1][2]["properties"]["Source"]["select"]["name"])

    def test_empty_optional_fields_are_omitted_not_sent_empty(self):
        api = FakeNotion()
        api.add("db1", make())
        props = api.calls[-1][2]["properties"]
        self.assertNotIn("Recruiter Contact", props)

    def test_term_named_in_the_role_is_written(self):
        api = FakeNotion()
        listing = make(role="Winter 2027 Co-op")
        api.add("db1", listing)
        props = api.calls[-1][2]["properties"]
        self.assertEqual(props["Term"]["select"]["name"], listing.term)
        self.assertEqual(listing.term, "Winter 2027")

    def test_unlabelled_role_writes_unspecified_term(self):
        api = FakeNotion()
        api.add("db1", make(role="Gameplay Programmer Intern"))
        props = api.calls[-1][2]["properties"]
        self.assertEqual(props["Term"]["select"]["name"], "Unspecified")

    def test_add_all_survives_one_failing_row(self):
        class Flaky(FakeNotion):
            def _call(self, method, path, **kw):
                if "bad" in str(kw.get("json", "")):
                    raise RuntimeError("boom")
                return super()._call(method, path, **kw)

        api = Flaky()
        good = make(company="Good Co")
        written = api.add_all("db1", [make(company="bad"), good])
        self.assertEqual(written, [good])


def _row(page_id, skills="", recruiter="", company="Acme"):
    """A minimal Notion query-result row, shaped like the real API response."""
    return {
        "id": page_id,
        "properties": {
            notion_sink.P_SKILLS: {"rich_text": [{"plain_text": skills}]} if skills else {"rich_text": []},
            notion_sink.P_RECRUITER: {"email": recruiter} if recruiter else {},
            notion_sink.P_COMPANY: {"rich_text": [{"plain_text": company}]},
            notion_sink.P_PORTAL: {"url": "https://example.com/job"},
        },
    }


class TestBackfill(unittest.TestCase):
    def test_only_rows_missing_skills_come_back(self):
        api = FakeNotion()
        complete = _row("complete", skills="Python", recruiter="a@b.com")
        missing_skills = _row("missing-skills", recruiter="a@b.com")
        missing_recruiter = _row("missing-recruiter", skills="Python")
        api.responses = [{"results": [complete, missing_skills, missing_recruiter],
                          "has_more": False}]

        out = api.rows_to_backfill("db1")

        page_ids = {row["page_id"] for row in out}
        self.assertEqual(page_ids, {"missing-skills"})

    def test_a_row_with_skills_but_no_recruiter_never_queues(self):
        # Scraped recruiter addresses are rare. Queueing on them pinned the
        # same handful of rows at the head of the queue run after run, so the
        # rows still saying "See posting" never got a turn.
        api = FakeNotion()
        api.responses = [{"results": [_row("has-skills", skills="Python")],
                          "has_more": False}]
        self.assertEqual(api.rows_to_backfill("db1"), [])

    def test_a_queued_row_still_picks_up_a_missing_recruiter(self):
        api = FakeNotion()
        api.responses = [{"results": [_row("no-skills")], "has_more": False}]
        out = api.rows_to_backfill("db1")
        self.assertTrue(out[0]["needs_skills"])
        self.assertTrue(out[0]["needs_recruiter"])

    def test_a_skipped_row_is_never_fetched(self):
        api = FakeNotion()
        skipped = _row("skipped")
        skipped["properties"][notion_sink.P_APPLIED] = {"select": {"name": "Skipped"}}
        api.responses = [{"results": [skipped, _row("open")], "has_more": False}]
        self.assertEqual([r["page_id"] for r in api.rows_to_backfill("db1")], ["open"])

    def test_limit_caps_how_many_rows_come_back(self):
        api = FakeNotion()
        rows = [_row(f"row-{i}") for i in range(5)]
        api.responses = [{"results": rows, "has_more": False}]

        out = api.rows_to_backfill("db1", limit=2)

        self.assertEqual(len(out), 2)


class TestUpdateRow(unittest.TestCase):
    def test_only_skills_and_recruiter_are_sent(self):
        api = FakeNotion()
        api.update_row("page1", skills="Unity, C#", recruiter="recruiter@studio.com")
        body = api.calls[-1][2]
        self.assertEqual(set(body["properties"]), {"Skill Requirements", "Recruiter Contact"})
        self.assertNotIn("Applied", body["properties"])
        self.assertNotIn("Notes", body["properties"])
        self.assertNotIn("Deadline", body["properties"])
        self.assertNotIn("My Resume PDF", body["properties"])


class TestExistingJobIdsPagination(unittest.TestCase):
    def test_ids_from_every_page_come_back(self):
        api = FakeNotion()
        page_one = {
            "results": [_page_with_job_id("id-1")],
            "has_more": True,
            "next_cursor": "cursor-abc",
        }
        page_two = {"results": [_page_with_job_id("id-2")], "has_more": False}
        api.responses = [page_one, page_two]

        ids = api.existing_job_ids("db1")

        self.assertEqual(ids, {"id-1", "id-2"})
        # The second query must have followed the cursor from the first page.
        second_call_body = api.calls[1][2]
        self.assertEqual(second_call_body["start_cursor"], "cursor-abc")


class TestExistingJobIdsFloor(unittest.TestCase):
    """A populated database reading as zero job ids is a schema incident, and
    treating it as "nothing is here yet" would re-append every listing.
    """

    def test_populated_database_with_no_readable_job_ids_aborts(self):
        api = FakeNotion()
        blank = {"properties": {notion_sink.P_JOB_ID: {"rich_text": []}}}
        api.responses = [{"results": [blank, blank], "has_more": False}]
        with self.assertRaises(RuntimeError) as caught:
            api.existing_job_ids("db1")
        self.assertIn("Job ID", str(caught.exception))

    def test_a_genuinely_empty_database_is_not_an_error(self):
        api = FakeNotion()
        api.responses = [{"results": [], "has_more": False}]
        self.assertEqual(api.existing_job_ids("db1"), set())

    def test_one_readable_id_among_blanks_is_enough_to_proceed(self):
        api = FakeNotion()
        blank = {"properties": {notion_sink.P_JOB_ID: {"rich_text": []}}}
        api.responses = [{"results": [blank, _page_with_job_id("id-1")],
                          "has_more": False}]
        self.assertEqual(api.existing_job_ids("db1"), {"id-1"})


def _page_with_job_id(job_id):
    return {"properties": {notion_sink.P_JOB_ID: {"rich_text": [{"plain_text": job_id}]}}}


def _digest_row(company, role, deadline="", applied="", applied_date=""):
    """A minimal query-result row shaped like the real API response."""
    props = {
        notion_sink.P_COMPANY: {"rich_text": [{"plain_text": company}]},
        notion_sink.P_TITLE: {"title": [{"plain_text": role}]},
    }
    if deadline:
        props[notion_sink.P_DEADLINE] = {"date": {"start": deadline}}
    if applied:
        props[notion_sink.P_APPLIED] = {"select": {"name": applied}}
    if applied_date:
        props[notion_sink.P_APPLIED_DATE] = {"date": {"start": applied_date}}
    return {"properties": props}


class TestRowsForDigest(unittest.TestCase):
    def test_rows_without_a_deadline_or_applied_status_are_excluded(self):
        api = FakeNotion()
        with_deadline = _digest_row("Riot Games", "Gameplay Intern", deadline="2026-10-01")
        neither = _digest_row("Acme", "SWE Intern")
        api.responses = [{"results": [with_deadline, neither], "has_more": False}]

        rows, total, applied_count = api.rows_for_digest("db1")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Company"], "Riot Games")
        self.assertEqual(rows[0]["Role"], "Gameplay Intern")
        self.assertEqual(rows[0]["Deadline"], "2026-10-01")
        # Both rows count toward the totals even though "neither" is filtered
        # out of `rows`: the summary line must reflect the whole database.
        self.assertEqual(total, 2)
        self.assertEqual(applied_count, 0)

    def test_applied_row_without_a_deadline_is_still_included(self):
        # Only a few percent of postings publish a machine-readable deadline,
        # so gating on Deadline alone would drop almost every follow-up case.
        api = FakeNotion()
        row = _digest_row("Riot Games", "Gameplay Intern", applied="Applied",
                          applied_date="2026-08-01")
        api.responses = [{"results": [row], "has_more": False}]

        rows, _total, _applied_count = api.rows_for_digest("db1")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Applied Date"], "2026-08-01")

    def test_applied_name_is_carried_through_for_the_digest_to_check(self):
        api = FakeNotion()
        row = _digest_row("Riot Games", "Gameplay Intern", deadline="2026-10-01", applied="Applied")
        api.responses = [{"results": [row], "has_more": False}]

        rows, _total, _applied_count = api.rows_for_digest("db1")

        self.assertEqual(rows[0]["Application"], "Applied")

    def test_pagination_follows_the_cursor(self):
        api = FakeNotion()
        page_one = {
            "results": [_digest_row("A", "Role A", deadline="2026-10-01")],
            "has_more": True,
            "next_cursor": "cursor-xyz",
        }
        page_two = {"results": [_digest_row("B", "Role B", deadline="2026-11-01")], "has_more": False}
        api.responses = [page_one, page_two]

        rows, _total, _applied_count = api.rows_for_digest("db1")

        self.assertEqual({r["Company"] for r in rows}, {"A", "B"})
        self.assertEqual(api.calls[1][2]["start_cursor"], "cursor-xyz")

    def test_totals_count_the_whole_database_across_pages(self):
        api = FakeNotion()
        page_one = {
            "results": [
                _digest_row("A", "Role A", deadline="2026-10-01"),
                _digest_row("B", "Role B", applied="Applied", applied_date="2026-08-01"),
            ],
            "has_more": True,
            "next_cursor": "cursor-xyz",
        }
        # Not returned in `rows` (no deadline, not applied) but still counted.
        page_two = {"results": [_digest_row("C", "Role C")], "has_more": False}
        api.responses = [page_one, page_two]

        rows, total, applied_count = api.rows_for_digest("db1")

        self.assertEqual(len(rows), 2)
        self.assertEqual(total, 3)
        self.assertEqual(applied_count, 1)


def _applied_row(page_id, applied="", has_date=False):
    """A minimal query-result row for the applied-date stamping queries."""
    props = {}
    if applied:
        props[notion_sink.P_APPLIED] = {"select": {"name": applied}}
    if has_date:
        props[notion_sink.P_APPLIED_DATE] = {"date": {"start": "2026-08-01"}}
    return {"id": page_id, "properties": props}


class TestRowsMissingAppliedDate(unittest.TestCase):
    def test_a_row_seen_applied_for_the_first_time_comes_back(self):
        api = FakeNotion()
        api.responses = [{"results": [_applied_row("page-1", applied="Applied")],
                          "has_more": False}]

        out = api.rows_missing_applied_date("db1")

        self.assertEqual([row["page_id"] for row in out], ["page-1"])

    def test_a_row_that_already_has_a_date_is_never_returned_again(self):
        api = FakeNotion()
        api.responses = [{"results": [_applied_row("page-1", applied="Applied", has_date=True)],
                          "has_more": False}]

        out = api.rows_missing_applied_date("db1")

        self.assertEqual(out, [])

    def test_a_row_flipped_back_from_applied_keeps_its_date(self):
        # Previously stamped, then Applied changed away from "Applied" -- the
        # date must survive the flip, so the row must not come back either way.
        api = FakeNotion()
        flipped_back = _applied_row("page-1", applied="Not applied", has_date=True)
        api.responses = [{"results": [flipped_back], "has_more": False}]

        out = api.rows_missing_applied_date("db1")

        self.assertEqual(out, [])

    def test_a_row_not_yet_applied_is_not_returned(self):
        api = FakeNotion()
        api.responses = [{"results": [_applied_row("page-1", applied="Not applied")],
                          "has_more": False}]

        out = api.rows_missing_applied_date("db1")

        self.assertEqual(out, [])


class TestStampAppliedDate(unittest.TestCase):
    def test_only_applied_date_is_sent(self):
        api = FakeNotion()
        api.stamp_applied_date("page-1", "2026-09-26")
        method, path, body = api.calls[-1]
        self.assertEqual((method, path), ("PATCH", "/pages/page-1"))
        self.assertEqual(set(body["properties"]), {"Applied Date"})
        self.assertEqual(body["properties"]["Applied Date"]["date"]["start"], "2026-09-26")


if __name__ == "__main__":
    unittest.main()


class TestDigestRowsCarryApplying(unittest.TestCase):
    def _api(self, applied, files):
        api = FakeNotion()
        api.responses = [{"results": [{"properties": {
            "Company": {"rich_text": [{"plain_text": "Epic Games"}]},
            "Title": {"title": [{"plain_text": "Gameplay Programmer Intern"}]},
            "Deadline": {"date": None},
            "Applied": {"select": {"name": applied}},
            "Applied Date": {"date": None},
            "My Resume PDF": {"files": files},
        }}], "has_more": False}]
        return api

    def test_an_applying_row_with_no_deadline_still_comes_back(self):
        rows, total, _ = self._api("Applying", []).rows_for_digest("db1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Application"], "Applying")
        self.assertFalse(rows[0]["Has Resume"])

    def test_an_attached_resume_is_reported(self):
        rows, _, _ = self._api("Applying", [{"name": "resume.pdf"}]).rows_for_digest("db1")
        self.assertTrue(rows[0]["Has Resume"])

    def test_a_skipped_row_stays_out_even_with_a_deadline(self):
        api = self._api("Skipped", [])
        api.responses[0]["results"][0]["properties"]["Deadline"] = {"date": {"start": "2026-10-10"}}
        rows, total, _ = api.rows_for_digest("db1")
        self.assertEqual(rows, [])
        self.assertEqual(total, 1)

    def test_a_plain_row_with_no_deadline_is_still_skipped(self):
        rows, total, _ = self._api("Not applied", []).rows_for_digest("db1")
        self.assertEqual(rows, [])
        self.assertEqual(total, 1)


class TestBackfillWidened(unittest.TestCase):
    """Backfill fills the empty enrichment fields, and nothing the user owns."""

    OWNED = {"Applied", "Applied Date", "Deadline", "My Resume PDF", "Title",
             "Company", "Job ID", "Application Portal", "Posted", "Source", "Category", "Term"}

    def test_update_row_can_never_touch_a_user_owned_property(self):
        api = FakeNotion()
        api.update_row("p1", skills="S", recruiter="r@x.com", keywords=["Unity"], notes="Pay: $30")
        sent = set(api.calls[-1][2]["properties"])
        self.assertEqual(sent, {"Skill Requirements", "Recruiter Contact",
                                "Resume Keywords", "Notes"})
        self.assertFalse(sent & self.OWNED)

    def test_nothing_is_sent_when_there_is_nothing_to_fill(self):
        api = FakeNotion()
        api.update_row("p1")
        self.assertEqual(api.calls, [])

    def test_only_the_named_fields_are_sent(self):
        api = FakeNotion()
        api.update_row("p1", notes="Pay: $30")
        self.assertEqual(set(api.calls[-1][2]["properties"]), {"Notes"})

    def _queue_api(self, rows):
        api = FakeNotion()
        api.responses = [{"results": rows, "has_more": False}]
        return api

    def _row(self, skills="", keywords=None, notes="", email=None):
        return {"id": "p", "properties": {
            "Application Portal": {"url": "https://x/1"},
            "Company": {"rich_text": [{"plain_text": "Acme"}]},
            "Skill Requirements": {"rich_text": [{"plain_text": skills}] if skills else []},
            "Resume Keywords": {"multi_select": keywords or []},
            "Notes": {"rich_text": [{"plain_text": notes}] if notes else []},
            "Recruiter Contact": {"email": email},
        }}

    def test_a_row_missing_only_keywords_does_not_earn_a_fetch(self):
        # Keywords come from the role title, so they never justify opening a page.
        api = self._queue_api([self._row(skills="S", notes="N", email="a@b.c")])
        self.assertEqual(api.rows_to_backfill("db1", 25), [])

    def test_a_row_missing_skills_is_queued(self):
        api = self._queue_api([self._row(notes="N", email="a@b.c")])
        self.assertEqual(len(api.rows_to_backfill("db1", 25)), 1)

    def test_a_fully_populated_row_is_not_queued(self):
        api = self._queue_api([self._row(skills="S", keywords=[{"name": "Unity"}],
                                         notes="N", email="a@b.c")])
        self.assertEqual(api.rows_to_backfill("db1", 25), [])

    def test_the_queue_reports_which_fields_are_missing(self):
        api = self._queue_api([self._row(email="a@b.c")])
        row = api.rows_to_backfill("db1", 25)[0]
        self.assertTrue(row["needs_skills"])
        self.assertTrue(row["needs_keywords"])
        self.assertTrue(row["needs_notes"])
        self.assertFalse(row["needs_recruiter"])

    def test_the_queue_works_oldest_first_so_it_cannot_stick(self):
        api = self._queue_api([self._row()])
        api.rows_to_backfill("db1", 25)
        body = api.calls[-1][2]
        self.assertEqual(body["sorts"][0]["direction"], "ascending")


class TestBlankRow(unittest.TestCase):
    """A row added by hand in Notion has an empty Applied select (null)."""

    def _api(self):
        api = FakeNotion()
        api.responses = [{"results": [{"id": "blank", "properties": {
            "Company": {"rich_text": []}, "Title": {"title": []},
            "Deadline": {"date": None}, "Applied": {"select": None},
            "Applied Date": {"date": None}, "My Resume PDF": {"files": []},
        }}], "has_more": False}]
        return api

    def test_digest_rows_survive_a_blank_row(self):
        rows, total, _ = self._api().rows_for_digest("db1")
        self.assertEqual((rows, total), ([], 1))

    def test_applied_date_scan_survives_a_blank_row(self):
        self.assertEqual(self._api().rows_missing_applied_date("db1"), [])
