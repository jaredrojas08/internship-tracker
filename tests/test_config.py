import unittest

import config
import studios


class TestTermFor(unittest.TestCase):
    def test_season_then_year(self):
        self.assertEqual(config.term_for("Western Digital Summer 2027 Intern"), "Summer 2027")

    def test_winter(self):
        self.assertEqual(config.term_for("Winter 2027 Co-op"), "Winter 2027")

    def test_fall(self):
        self.assertEqual(config.term_for("Fall 2027 Software Engineer Intern"), "Fall 2027")

    def test_spring(self):
        self.assertEqual(config.term_for("Spring 2027 Intern"), "Spring 2027")

    def test_summer_2026(self):
        self.assertEqual(config.term_for("Summer 2026 Intern"), "Summer 2026")

    def test_year_then_season_ordering(self):
        self.assertEqual(config.term_for("2027 Summer Software Engineer Intern"), "Summer 2027")

    def test_two_letter_season_code_shorthand(self):
        self.assertEqual(config.term_for("Su27 Software Engineer Intern"), "Summer 2027")

    def test_apostrophe_year_shorthand(self):
        self.assertEqual(config.term_for("Software Engineer Intern - Summer '27"), "Summer 2027")

    def test_case_insensitive(self):
        self.assertEqual(config.term_for("SUMMER 2027 INTERN"), "Summer 2027")

    def test_unlabelled_title_is_unspecified(self):
        self.assertEqual(config.term_for("Software Engineer Intern"), "Unspecified")


if __name__ == "__main__":
    unittest.main()


class TestRoleFilterKeepsTechRoles(unittest.TestCase):
    """Titles seen dropped by the live aggregators in Oct 2026 with no keyword match."""

    def test_tech_titles_without_software_or_engineer_are_kept(self):
        for title in ["Tools Programmer Intern", "Engine Programmer Intern",
                      "DevOps Intern", "Site Reliability Internship - Spring 2027",
                      "Cloud Security Intern", "Firmware Intern",
                      "Embedded Firmware Intern - Summer 2027",
                      "Embedded SDET Co-Op Full Time Intern January-June 2027",
                      "Global Technology Summer Analyst, Cybersecurity Analyst"]:
            self.assertTrue(config.matches_role_filter(title), title)

    def test_finance_and_operations_titles_stay_out(self):
        for title in ["Macro Analyst Intern (Summer 2027, June start)",
                      "Operations Management Intern", "Equity Research Analyst Intern"]:
            self.assertFalse(config.matches_role_filter(title), title)


class TestStudioBoardRelevance(unittest.TestCase):
    US = "Cary, North Carolina, United States"

    def test_non_tech_functions_are_dropped(self):
        for title in ["Communications Intern", "EA SPORTS Communications Intern - Summer 2027",
                      "Legal Specialist Intern, Approvals (JD) - Summer 2027",
                      "Customer Experience & Operations Intern - Summer 2027"]:
            self.assertFalse(studios.is_relevant(title, self.US), title)

    def test_craft_and_adjacent_roles_are_kept(self):
        for title in ["Tech Art Intern", "VFX Intern", "Level Design Intern",
                      "Analytics Intern", "Product Management Intern", "Data Science Intern"]:
            self.assertTrue(studios.is_relevant(title, self.US), title)

    def test_a_tech_title_in_a_non_tech_area_is_kept(self):
        self.assertTrue(studios.is_relevant("Software Engineer Intern, Communications", self.US))


class TestStudioInternTitles(unittest.TestCase):
    US = "Playa Vista"

    def test_plural_internships_and_co_ops_are_intern_titles(self):
        # Activision titles every posting "2027 Summer Internships - <track>".
        for title in ["Activision 2027 Summer Internships - Game Engineering",
                      "Activision 2027 Summer Internships - Software Engineering",
                      "2027 Winter Co-Ops - Software Development"]:
            self.assertTrue(studios.is_relevant(title, self.US), title)

    def test_mba_internship_is_still_dropped(self):
        self.assertFalse(studios.is_relevant("Activision 2027 Summer Internships - MBA", self.US))


class TestNothingDropsSilently(unittest.TestCase):
    """A posting no rule recognises goes to Notion flagged for review, not away."""

    def _speedy(self, role):
        import sources
        listings, _ = sources._parse_speedy_table(
            ["Company", "Position", "Posting"],
            [["Acme", role, '<a href="https://jobs.example.com/acme/123">Apply</a>']])
        return listings

    def test_unrecognised_aggregator_title_is_kept_for_review(self):
        listings = self._speedy("Build Wrangler Intern")
        self.assertEqual(len(listings), 1)
        self.assertTrue(listings[0].needs_review)

    def test_recognised_title_is_not_flagged(self):
        self.assertFalse(self._speedy("Software Engineer Intern")[0].needs_review)

    def test_deliberate_rules_still_drop(self):
        self.assertEqual(self._speedy("Research Intern (PhD)"), [])

    def test_studio_title_that_only_looks_early_career_is_flagged(self):
        self.assertTrue(studios.needs_review("2027 Summer Trainee - Gameplay", "Playa Vista"))
        self.assertTrue(studios.needs_review("Student Programmer, Summer 2027", "Austin, TX"))

    def test_studio_full_time_titles_are_not_flagged(self):
        for title in ["International Operations Manager", "Internal Tools Engineer",
                      "Senior Gameplay Engineer",
                      "Principal Engineer, Enterprise AI — Technical Fellow Office"]:
            self.assertFalse(studios.needs_review(title, "Irvine, CA"), title)

    def test_review_rows_get_their_own_category(self):
        import enrich
        import parser as md
        listing = md.Listing("Acme", "Build Wrangler Intern", "", "https://x", needs_review=True)
        self.assertEqual(enrich.category_for(listing), "Check: filtered out")


class TestBoardProblemsRecorded(unittest.TestCase):
    def test_failed_and_empty_boards_are_recorded(self):
        from unittest import mock
        # Keyed by board, not call order: boards are fetched on parallel threads.
        results = {"good": ("Good", [("SWE Intern", "Austin, TX", "https://x/1")], ""),
                   "empty": ("Empty", [], ""), "broken": ("Broken", [], "HTTP 404")}
        with mock.patch.object(studios, "_fetch_board", side_effect=results.get), \
             mock.patch.object(studios, "STUDIO_BOARDS", ["good", "empty", "broken"]):
            studios.fetch_studio_listings()
        self.assertEqual(studios.board_problems,
                         ["Empty (0 jobs on the board)", "Broken (HTTP 404)"])
