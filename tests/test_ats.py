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

import requests

import ats

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

WORKDAY_URL = "https://bah.wd1.myworkdayjobs.com/BAH_Jobs/job/City/Title_R123"


class _FakeResponse:
    """Stand-in for requests.Response, controlled per test. No network involved."""

    def __init__(self, status_ok=True, text="", json_data=None, json_error=False):
        self.text = text
        self._json_data = json_data
        self._json_error = json_error
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise requests.exceptions.HTTPError("bad status")

    def json(self):
        if self._json_error:
            raise ValueError("invalid json")
        return self._json_data


class _FakeSession:
    """Stand-in for requests.Session: either returns a canned response or raises."""

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc

    def get(self, url, timeout=None, headers=None):
        if self._raise_exc:
            raise self._raise_exc
        return self._response


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


class TestWorkdayApiUrl(unittest.TestCase):
    def test_non_workday_url_returns_empty_string(self):
        url = "https://job-boards.greenhouse.io/riotgames/jobs/7977844"
        self.assertEqual(ats._workday_api_url(url), "")

    def test_bare_tenant_url_produces_cxs_endpoint(self):
        url = "https://bah.wd1.myworkdayjobs.com/BAH_Jobs/job/Annapolis-Junction-MD/Systems-Administrator-Intern_R0249565"
        expected = (
            "https://bah.wd1.myworkdayjobs.com/wday/cxs/bah/BAH_Jobs"
            "/job/Annapolis-Junction-MD/Systems-Administrator-Intern_R0249565"
        )
        self.assertEqual(ats._workday_api_url(url), expected)

    def test_locale_prefixed_url_produces_cxs_endpoint(self):
        url = "https://xboxgaming.wd1.myworkdayjobs.com/en-US/Blizzard_External_Careers/job/Some-City/Some-Title_R123"
        expected = (
            "https://xboxgaming.wd1.myworkdayjobs.com/wday/cxs/xboxgaming"
            "/Blizzard_External_Careers/job/Some-City/Some-Title_R123"
        )
        self.assertEqual(ats._workday_api_url(url), expected)


class TestFetchPage(unittest.TestCase):
    def test_empty_url_returns_not_ok(self):
        result = ats.fetch_page("", _FakeSession())
        self.assertFalse(result.ok)

    def test_malformed_url_does_not_raise(self):
        session = _FakeSession(raise_exc=requests.exceptions.MissingSchema("no scheme"))
        result = ats.fetch_page("not-a-url", session)
        self.assertFalse(result.ok)

    def test_non_http_scheme_does_not_raise(self):
        session = _FakeSession(raise_exc=requests.exceptions.InvalidSchema("bad scheme"))
        result = ats.fetch_page("ftp://example.com/job", session)
        self.assertFalse(result.ok)

    def test_session_get_raising_does_not_raise(self):
        session = _FakeSession(raise_exc=ConnectionError("network down"))
        result = ats.fetch_page("https://example.com/job", session)
        self.assertFalse(result.ok)

    def test_non_2xx_response_returns_not_ok(self):
        session = _FakeSession(response=_FakeResponse(status_ok=False))
        result = ats.fetch_page("https://example.com/job", session)
        self.assertFalse(result.ok)

    def test_workday_invalid_json_returns_not_ok(self):
        session = _FakeSession(response=_FakeResponse(status_ok=True, json_error=True))
        result = ats.fetch_page(WORKDAY_URL, session)
        self.assertFalse(result.ok)

    def test_large_body_with_near_empty_text_is_not_ok(self):
        # Unclosed <script> makes HTMLParser treat the rest of the page as
        # CDATA, so it silently vanishes. That must not read as a successful fetch.
        body = "<script>" + ("x" * 3000)
        session = _FakeSession(response=_FakeResponse(status_ok=True, text=body))
        result = ats.fetch_page("https://example.com/job", session)
        self.assertFalse(result.ok)

    def test_workday_truncated_description_is_not_ok(self):
        body = "<script>" + ("x" * 3000)
        payload = {"jobPostingInfo": {"jobDescription": body}}
        session = _FakeSession(response=_FakeResponse(status_ok=True, json_data=payload))
        result = ats.fetch_page(WORKDAY_URL, session)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
