import unittest

import migrate


ROW = {
    "Company": "Riot Games", "Role": "Gameplay Programmer Intern",
    "Location": "Los Angeles, CA",
    "Apply Link": "https://boards.greenhouse.io/riotgames/jobs/1234567",
    "Salary": "", "Deadline": "2026-12-31", "Date Added": "2026-09-19",
    "Remote?": "NO", "Game?": "YES", "Source": "studios",
    "Application": "Applying", "Applied Date": "", "Remove?": "FALSE",
}


class TestMigration(unittest.TestCase):
    def test_carries_the_hand_typed_deadline(self):
        self.assertEqual(migrate.rows_to_listings([ROW])[0].deadline, "2026-12-31")

    def test_date_added_becomes_posted_at_with_first_seen_precision(self):
        listing = migrate.rows_to_listings([ROW])[0]
        self.assertEqual(listing.posted_at.date().isoformat(), "2026-09-19")
        self.assertEqual(listing.posted_precision, "first_seen")

    def test_posted_at_is_timezone_aware(self):
        self.assertIsNotNone(migrate.rows_to_listings([ROW])[0].posted_at.tzinfo)

    def test_game_column_restores_the_studio_flag(self):
        self.assertTrue(migrate.rows_to_listings([ROW])[0].is_game)

    def test_rows_marked_remove_are_dropped(self):
        row = dict(ROW, **{"Remove?": "TRUE"})
        self.assertEqual(migrate.rows_to_listings([row]), [])

    def test_blank_rows_are_dropped(self):
        self.assertEqual(migrate.rows_to_listings([{k: "" for k in ROW}]), [])


if __name__ == "__main__":
    unittest.main()
