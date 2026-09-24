import unittest
from datetime import timezone

import ats

POSTING = """
About the role
We are looking for an intern to join the gameplay team.

Basic Qualifications
- Currently pursuing a BS in Computer Science
- Experience with C# and Unity
- Graduating between December 2027 and June 2028

Benefits
We offer medical, dental and a 401(k) match.

Equal Employment Opportunity
We are an equal opportunity employer. Accommodations will be provided.
"""


class TestRequirements(unittest.TestCase):
    def test_lifts_the_qualifications_section(self):
        out = ats.extract_requirements(POSTING)
        self.assertIn("BS in Computer Science", out)
        self.assertIn("C# and Unity", out)

    def test_stops_before_benefits(self):
        out = ats.extract_requirements(POSTING)
        self.assertNotIn("401(k)", out)

    def test_drops_eeo_boilerplate(self):
        out = ats.extract_requirements(POSTING)
        self.assertNotIn("equal opportunity", out.lower())

    def test_no_recognised_heading_returns_empty(self):
        self.assertEqual(ats.extract_requirements("Just some prose about us."), "")

    def test_truncates_to_max_chars(self):
        long_text = "Requirements\n" + ("- a very long bullet line\n" * 200)
        self.assertLessEqual(len(ats.extract_requirements(long_text, max_chars=300)), 300)


class TestContactEmail(unittest.TestCase):
    def test_prefers_a_hiring_local_part(self):
        html = '<p>legal@acme.com</p><p>campus.recruiting@acme.com</p>'
        self.assertEqual(ats.extract_contact_email(html), "campus.recruiting@acme.com")

    def test_discards_ats_vendor_addresses(self):
        html = '<p>no-reply@greenhouse.io</p><p>support@lever.co</p>'
        self.assertEqual(ats.extract_contact_email(html), "")

    def test_discards_automated_senders(self):
        self.assertEqual(ats.extract_contact_email("<p>do-not-reply@acme.com</p>"), "")

    def test_returns_empty_when_no_address(self):
        self.assertEqual(ats.extract_contact_email("<p>Apply on our site.</p>"), "")

    def test_never_constructs_an_address(self):
        # A page naming a person but no address must not yield firstname@company
        self.assertEqual(ats.extract_contact_email("<p>Contact Jane Doe at Acme.</p>"), "")


class TestNotes(unittest.TestCase):
    def test_captures_pay_when_stated(self):
        out = ats.extract_notes("The projected compensation range is $53,000.00 to $108,000.00.")
        self.assertIn("$53,000", out)

    def test_captures_sponsorship_statement(self):
        out = ats.extract_notes("This role is not eligible for visa sponsorship.")
        self.assertIn("sponsorship", out.lower())

    def test_returns_empty_when_nothing_notable(self):
        self.assertEqual(ats.extract_notes("We build games."), "")


class TestPostedAt(unittest.TestCase):
    def test_reads_schema_org_dateposted_with_time(self):
        html = '<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-09-20T14:30:00Z"}</script>'
        when, precision = ats.extract_posted_at(html)
        self.assertIsNotNone(when)
        self.assertEqual(when.tzinfo, timezone.utc)
        self.assertEqual(precision, "scraped")

    def test_bare_date_is_marked_day_precision(self):
        html = '<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-09-20"}</script>'
        when, precision = ats.extract_posted_at(html)
        self.assertIsNotNone(when)
        self.assertEqual(precision, "day")

    def test_no_block_returns_none(self):
        when, precision = ats.extract_posted_at("<p>nothing here</p>")
        self.assertIsNone(when)
        self.assertEqual(precision, "unknown")


if __name__ == "__main__":
    unittest.main()
