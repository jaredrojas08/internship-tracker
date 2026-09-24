import unittest
from unittest import mock

import ats
import enrich
import parser as md


def make(**kw):
    base = dict(company="Riot Games", role="Gameplay Programmer Intern",
                location="Los Angeles, CA", apply_url="https://example.com/jobs/1234567",
                source="studios", from_game_studio=True)
    base.update(kw)
    return md.Listing(**base)


class TestEnrich(unittest.TestCase):
    def test_unreadable_page_falls_back_to_see_posting(self):
        listing = make()
        with mock.patch.object(ats, "fetch_page", return_value=ats.PageData(ok=False)):
            enrich.enrich_all([listing])
        self.assertEqual(listing.skills, [enrich.GENERIC_SKILLS])

    def test_unreadable_page_still_gets_category_keywords(self):
        listing = make()
        with mock.patch.object(ats, "fetch_page", return_value=ats.PageData(ok=False)):
            enrich.enrich_all([listing])
        self.assertTrue(listing.resume_keywords)

    def test_readable_page_uses_the_employers_words(self):
        page = ats.PageData(text="Qualifications\n- Experience with Unity and C#\n", ok=True)
        listing = make()
        with mock.patch.object(ats, "fetch_page", return_value=page):
            enrich.enrich_all([listing])
        self.assertIn("Unity", listing.skills_cell())

    def test_enrichment_never_raises_on_a_bad_listing(self):
        listing = make(apply_url="")
        enrich.enrich_all([listing])  # must not raise
        self.assertEqual(listing.skills, [enrich.GENERIC_SKILLS])

    def test_category_is_set_from_the_role(self):
        listing = make()
        with mock.patch.object(ats, "fetch_page", return_value=ats.PageData(ok=False)):
            enrich.enrich_all([listing])
        self.assertEqual(listing.category, "Game Programming")

    def test_non_game_role_gets_software_engineering_category(self):
        listing = make(role="Backend Software Engineer Intern", source="speedyapply",
                       from_game_studio=False)
        with mock.patch.object(ats, "fetch_page", return_value=ats.PageData(ok=False)):
            enrich.enrich_all([listing])
        self.assertEqual(listing.category, "Software Engineering")

    def test_enrich_all_survives_a_listing_that_raises_and_still_enriches_the_rest(self):
        bad = make(apply_url="https://example.com/jobs/bad")
        good = make(apply_url="https://example.com/jobs/good")
        good_page = ats.PageData(text="Qualifications\n- Experience with Unity and C#\n", ok=True)

        def fake_fetch(url, session):
            if url == bad.apply_url:
                raise RuntimeError("boom")
            return good_page

        with mock.patch.object(ats, "fetch_page", side_effect=fake_fetch):
            enrich.enrich_all([bad, good])  # must not raise

        # the listing whose fetch blew up still carries usable defaults
        self.assertTrue(bad.category)
        self.assertTrue(bad.resume_keywords)
        self.assertEqual(bad.skills, [enrich.GENERIC_SKILLS])

        # the other listing in the same batch was fully enriched regardless
        self.assertIn("Unity", good.skills_cell())

    def test_readable_page_fills_notes_recruiter_and_posted_time(self):
        text = (
            "Qualifications\n"
            "- Experience with Unity and C#\n\n"
            "This internship pays $25/hour.\n"
        )
        html = (
            "<html><body>"
            "<p>Contact our campus recruiting team at campus.recruiting@example-studio.com.</p>"
            '<script type="application/ld+json">{"datePosted": "2026-09-01T12:00:00Z"}</script>'
            "</body></html>"
        )
        page = ats.PageData(text=text, html=html, ok=True)
        listing = make()
        with mock.patch.object(ats, "fetch_page", return_value=page):
            enrich.enrich_all([listing])

        self.assertIn("$25/hour", listing.notes)
        self.assertEqual(listing.recruiter, "campus.recruiting@example-studio.com")
        self.assertEqual(listing.posted_precision, "scraped")
        self.assertEqual((listing.posted_at.year, listing.posted_at.month, listing.posted_at.day),
                         (2026, 9, 1))


if __name__ == "__main__":
    unittest.main()
