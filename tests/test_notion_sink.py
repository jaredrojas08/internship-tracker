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

    def test_add_all_survives_one_failing_row(self):
        class Flaky(FakeNotion):
            def _call(self, method, path, **kw):
                if "bad" in str(kw.get("json", "")):
                    raise RuntimeError("boom")
                return super()._call(method, path, **kw)

        api = Flaky()
        written = api.add_all("db1", [make(company="bad"), make(company="Good Co")])
        self.assertEqual(written, 1)


if __name__ == "__main__":
    unittest.main()
