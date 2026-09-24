import unittest

import config


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
