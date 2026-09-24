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
                    "My Resume PDF", "Source", "Job ID", "Notes", "Niche", "Deadline"}
        self.assertEqual(set(notion_sink.SCHEMA), expected)

    def test_applied_options_match_the_agreed_labels_and_colours(self):
        self.assertEqual(
            [(o["name"], o["color"]) for o in notion_sink.APPLIED_OPTIONS],
            [("Not applied", "default"), ("Applying", "yellow"), ("Applied", "blue"),
             ("Interviewing", "purple"), ("Offer", "green"), ("Rejected", "red")],
        )

    def test_deadline_is_a_date_not_text(self):
        self.assertIn("date", notion_sink.SCHEMA["Deadline"])

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
        written = api.add_all("db1", [make(company="bad"), make(company="Good Co")])
        self.assertEqual(written, 1)


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


if __name__ == "__main__":
    unittest.main()
