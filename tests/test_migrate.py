import unittest

import migrate
import notion_sink


ROW = {
    "Company": "Riot Games", "Role": "Gameplay Programmer Intern",
    "Location": "Los Angeles, CA",
    "Apply Link": "https://boards.greenhouse.io/riotgames/jobs/1234567",
    "Salary": "", "Deadline": "2026-12-31", "Date Added": "2026-09-19",
    "Remote?": "NO", "Game?": "YES", "Source": "studios",
    "Application": "Applying", "Applied Date": "", "Remove?": "FALSE",
}

PROGRAM_ROW = {
    "Organization": "Google", "Opportunity": "STEP Internship",
    "Link": "https://example.com/step", "Type": "Fellowship",
    "Deadline": "rolling", "Date Added": "2026-09-19",
    "Applied?": "FALSE", "Remove?": "FALSE",
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

    def test_non_iso_deadline_lands_in_notes_instead_of_the_date_field(self):
        # A hand-typed "rolling" or "check site" deadline has no date property
        # to go in; sending it through unchecked would 400 the whole write.
        row = dict(ROW, **{"Deadline": "rolling"})
        listing = migrate.rows_to_listings([row])[0]
        self.assertEqual(listing.deadline, "")
        self.assertIn("rolling", listing.notes)


class TestProgramsToListings(unittest.TestCase):
    def test_organization_opportunity_and_type_map_onto_listing_fields(self):
        listing = migrate.programs_to_listings([PROGRAM_ROW])[0]
        self.assertEqual(listing.company, "Google")
        self.assertEqual(listing.role, "STEP Internship")
        self.assertEqual(listing.category, "Program / Fellowship")
        self.assertIn("Fellowship", listing.notes)

    def test_rolling_deadline_lands_in_notes_with_no_date(self):
        listing = migrate.programs_to_listings([PROGRAM_ROW])[0]
        self.assertEqual(listing.deadline, "")
        self.assertIn("rolling", listing.notes)

    def test_check_site_deadline_lands_in_notes_with_no_date(self):
        row = dict(PROGRAM_ROW, **{"Deadline": "check site"})
        listing = migrate.programs_to_listings([row])[0]
        self.assertEqual(listing.deadline, "")
        self.assertIn("check site", listing.notes)

    def test_real_iso_deadline_is_kept_as_a_date(self):
        row = dict(PROGRAM_ROW, **{"Deadline": "2026-06-14"})
        listing = migrate.programs_to_listings([row])[0]
        self.assertEqual(listing.deadline, "2026-06-14")

    def test_rows_marked_remove_are_dropped(self):
        row = dict(PROGRAM_ROW, **{"Remove?": "TRUE"})
        self.assertEqual(migrate.programs_to_listings([row]), [])

    def test_blank_rows_are_dropped(self):
        self.assertEqual(migrate.programs_to_listings([{k: "" for k in PROGRAM_ROW}]), [])


class TestAppliedLabel(unittest.TestCase):
    VALID_OPTIONS = {option["name"] for option in notion_sink.APPLIED_OPTIONS}

    def test_known_sheet_values_map_to_notions_own_labels(self):
        self.assertEqual(migrate._applied_label("Not Applied"), "Not applied")
        self.assertEqual(migrate._applied_label("Applying"), "Applying")
        self.assertEqual(migrate._applied_label("Applied"), "Applied")

    def test_blank_and_unrecognised_values_fall_back_to_not_applied(self):
        self.assertEqual(migrate._applied_label(""), "Not applied")
        self.assertEqual(migrate._applied_label("Ghosted"), "Not applied")

    def test_every_mapped_value_is_one_of_notions_six_select_options(self):
        for raw in ("Not Applied", "Applying", "Applied", "", "Ghosted"):
            with self.subTest(raw=raw):
                self.assertIn(migrate._applied_label(raw), self.VALID_OPTIONS)


class _FakeApi:
    """Records job ids passed to add() instead of calling Notion."""

    def __init__(self):
        self.added_job_ids = []

    def add(self, database_id, listing):
        self.added_job_ids.append(listing.job_id)


class TestWriteAll(unittest.TestCase):
    def test_duplicate_job_id_in_the_same_batch_is_written_once(self):
        # Same Apply Link twice -> same job_id, as if the sheet had a repeated row.
        listings = migrate.rows_to_listings([ROW, ROW])
        api = _FakeApi()

        written = migrate.write_all(api, "db1", listings, seen=set())

        self.assertEqual(written, 1)
        self.assertEqual(len(api.added_job_ids), 1)


if __name__ == "__main__":
    unittest.main()
