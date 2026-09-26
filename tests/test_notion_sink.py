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
                    "My Resume PDF", "Source", "Job ID", "Notes", "Niche", "Deadline",
                    "Applied Date"}
        self.assertEqual(set(notion_sink.SCHEMA), expected)

    def test_applied_options_match_the_agreed_labels_and_colours(self):
        self.assertEqual(
            [(o["name"], o["color"]) for o in notion_sink.APPLIED_OPTIONS],
            [("Not applied", "default"), ("Applying", "yellow"), ("Applied", "blue"),
             ("Interviewing", "purple"), ("Offer", "green"), ("Rejected", "red")],
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

    def test_studio_listing_is_marked_niche(self):
        api = FakeNotion()
        api.add("db1", make(source="studios"))
        self.assertTrue(api.calls[-1][2]["properties"]["Niche"]["checkbox"])

    def test_aggregator_listing_is_not_niche(self):
        api = FakeNotion()
        api.add("db1", make(source="speedyapply", from_game_studio=False))
        self.assertFalse(api.calls[-1][2]["properties"]["Niche"]["checkbox"])

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
    def test_only_rows_missing_skills_or_recruiter_come_back(self):
        api = FakeNotion()
        complete = _row("complete", skills="Python", recruiter="a@b.com")
        missing_skills = _row("missing-skills", recruiter="a@b.com")
        missing_recruiter = _row("missing-recruiter", skills="Python")
        api.responses = [{"results": [complete, missing_skills, missing_recruiter],
                          "has_more": False}]

        out = api.rows_to_backfill("db1")

        page_ids = {row["page_id"] for row in out}
        self.assertEqual(page_ids, {"missing-skills", "missing-recruiter"})

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

        rows = api.rows_for_digest("db1")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Company"], "Riot Games")
        self.assertEqual(rows[0]["Role"], "Gameplay Intern")
        self.assertEqual(rows[0]["Deadline"], "2026-10-01")

    def test_applied_row_without_a_deadline_is_still_included(self):
        # Only a few percent of postings publish a machine-readable deadline,
        # so gating on Deadline alone would drop almost every follow-up case.
        api = FakeNotion()
        row = _digest_row("Riot Games", "Gameplay Intern", applied="Applied",
                          applied_date="2026-08-01")
        api.responses = [{"results": [row], "has_more": False}]

        rows = api.rows_for_digest("db1")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Applied Date"], "2026-08-01")

    def test_applied_name_is_carried_through_for_the_digest_to_check(self):
        api = FakeNotion()
        row = _digest_row("Riot Games", "Gameplay Intern", deadline="2026-10-01", applied="Applied")
        api.responses = [{"results": [row], "has_more": False}]

        rows = api.rows_for_digest("db1")

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

        rows = api.rows_for_digest("db1")

        self.assertEqual({r["Company"] for r in rows}, {"A", "B"})
        self.assertEqual(api.calls[1][2]["start_cursor"], "cursor-xyz")


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
