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


if __name__ == "__main__":
    unittest.main()
