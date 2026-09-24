"""Tests for ats.py, the shared protocol layer for posting pages.

Fixture note: the brief's two capture URLs (a Riot Games Greenhouse posting
and a Booz Allen Workday posting) had both closed by 2026-09-24. Substituted
live postings from the same ATS:
  - Greenhouse: https://job-boards.greenhouse.io/riotgames/jobs/7977844
  - Workday:    https://bah.wd1.myworkdayjobs.com/wday/cxs/bah/bah_jobs/job/
                Annapolis-Junction-MD/Systems-Administrator-Intern_R0249565
    (fetched with GET; a POST to this same URL returns HTTP 400, unlike the
    board-listing endpoint in studios.py, which does require POST)
"""

import json
import pathlib
import unittest

import ats

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class TestHtmlToText(unittest.TestCase):
    def test_strips_tags_and_keeps_words(self):
        text = ats.html_to_text("<div><p>Hello <b>world</b></p></div>")
        self.assertIn("Hello", text)
        self.assertIn("world", text)
        self.assertNotIn("<", text)

    def test_drops_script_and_style_content(self):
        text = ats.html_to_text("<style>.a{color:red}</style><script>var x=1</script><p>Keep</p>")
        self.assertIn("Keep", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("var x", text)

    def test_unescapes_entities(self):
        self.assertIn("R&D", ats.html_to_text("<p>R&amp;D</p>"))

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(ats.html_to_text(""), "")


class TestRealPayloads(unittest.TestCase):
    def test_greenhouse_page_yields_substantial_text(self):
        html = (FIXTURES / "greenhouse_job.html").read_text(encoding="utf-8")
        text = ats.html_to_text(html)
        self.assertGreater(len(text), 500)

    def test_workday_json_yields_description_text(self):
        payload = json.loads((FIXTURES / "workday_job.json").read_text(encoding="utf-8"))
        text = ats.workday_description(payload)
        self.assertGreater(len(text), 500)
        self.assertNotIn("<li>", text)


if __name__ == "__main__":
    unittest.main()
