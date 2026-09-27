import unittest
from unittest import mock

import ats
import enrich
import parser as md


def make_program(**kw):
    base = dict(org="Google", opportunity="STEP Internship",
                link="https://example.com/step", type="Fellowship",
                deadline="rolling")
    base.update(kw)
    return md.Program(**base)


class TestProgramsToListings(unittest.TestCase):
    def test_org_opportunity_and_link_map_onto_listing_fields(self):
        listing = md.programs_to_listings([make_program()])[0]
        self.assertEqual(listing.company, "Google")
        self.assertEqual(listing.role, "STEP Internship")
        self.assertEqual(listing.apply_url, "https://example.com/step")

    def test_category_is_always_program_or_fellowship(self):
        listing = md.programs_to_listings([make_program()])[0]
        self.assertEqual(listing.category, "Program / Fellowship")

    def test_type_folds_into_notes(self):
        listing = md.programs_to_listings([make_program(type="Fellowship")])[0]
        self.assertIn("Fellowship", listing.notes)

    def test_rolling_deadline_lands_in_notes_with_no_date(self):
        listing = md.programs_to_listings([make_program(deadline="rolling")])[0]
        self.assertEqual(listing.deadline, "")
        self.assertIn("rolling", listing.notes)

    def test_check_site_deadline_lands_in_notes_with_no_date(self):
        listing = md.programs_to_listings([make_program(deadline="check site")])[0]
        self.assertEqual(listing.deadline, "")
        self.assertIn("check site", listing.notes)

    def test_real_iso_deadline_is_kept_as_a_date_and_not_duplicated_in_notes(self):
        listing = md.programs_to_listings([make_program(deadline="2026-06-14")])[0]
        self.assertEqual(listing.deadline, "2026-06-14")
        self.assertNotIn("2026-06-14", listing.notes)

    def test_no_deadline_leaves_notes_to_just_the_type(self):
        listing = md.programs_to_listings([make_program(type="Fellowship", deadline="")])[0]
        self.assertEqual(listing.notes, "Fellowship")


class TestProgramCategorySurvivesEnrichment(unittest.TestCase):
    """internship_tracker.main() enriches the fetched listings, then appends
    program listings afterward -- never the other way around. enrich_all
    unconditionally recomputes category from is_game and re-fetches the apply
    page, so a program listing run through it would lose "Program / Fellowship"
    and the notes programs_to_listings set. This pins that ordering contract.
    """

    def test_program_listing_keeps_its_category_and_notes_when_appended_after_enrich(self):
        regular = md.Listing(
            company="Riot Games", role="Software Engineer Intern", location="LA",
            apply_url="https://boards.greenhouse.io/riotgames/jobs/1234567",
        )
        program = make_program(type="Fellowship", deadline="rolling")

        listings = [regular]
        with mock.patch("enrich.ats.fetch_page", return_value=ats.PageData(ok=False)):
            enrich.enrich_all(listings)
        listings = listings + md.programs_to_listings([program])

        program_listing = listings[-1]
        self.assertEqual(program_listing.category, "Program / Fellowship")
        self.assertIn("Fellowship", program_listing.notes)

    def test_running_enrich_all_on_a_program_listing_would_clobber_it(self):
        """Documents why the order matters: this is what NOT doing it looks like."""
        program_listing = md.programs_to_listings([make_program(type="Fellowship")])[0]
        with mock.patch("enrich.ats.fetch_page", return_value=ats.PageData(ok=False)):
            enrich.enrich_all([program_listing])
        self.assertNotEqual(program_listing.category, "Program / Fellowship")


if __name__ == "__main__":
    unittest.main()
