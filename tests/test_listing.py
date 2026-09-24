import unittest
from datetime import datetime, timezone

import parser as md


def make(**kw):
    base = dict(company="Riot Games", role="Software Engineer Intern",
                location="Los Angeles, CA",
                apply_url="https://boards.greenhouse.io/riotgames/jobs/1234567")
    base.update(kw)
    return md.Listing(**base)


class TestJobId(unittest.TestCase):
    def test_job_id_is_stable_across_url_cosmetics(self):
        a = make(apply_url="https://boards.greenhouse.io/riotgames/jobs/1234567")
        b = make(apply_url="https://job-boards.greenhouse.io/riotgames/jobs/1234567?gh_jid=1234567")
        self.assertEqual(a.job_id, b.job_id)

    def test_job_id_differs_for_different_postings(self):
        a = make(apply_url="https://boards.greenhouse.io/riotgames/jobs/1234567")
        b = make(apply_url="https://boards.greenhouse.io/riotgames/jobs/7654321")
        self.assertNotEqual(a.job_id, b.job_id)

    def test_job_id_is_a_plain_string(self):
        self.assertIsInstance(make().job_id, str)
        self.assertTrue(make().job_id)


class TestNiche(unittest.TestCase):
    def test_studio_board_is_niche(self):
        self.assertTrue(make(source="studios").is_niche())

    def test_github_aggregator_is_not_niche(self):
        self.assertFalse(make(source="speedyapply").is_niche())
        self.assertFalse(make(source="sndsh404").is_niche())


class TestNewFields(unittest.TestCase):
    def test_defaults_are_empty_not_none(self):
        listing = make()
        self.assertEqual(listing.resume_keywords, [])
        self.assertEqual(listing.skills, [])
        self.assertEqual(listing.recruiter, "")
        self.assertEqual(listing.notes, "")
        self.assertEqual(listing.deadline, "")
        self.assertIsNone(listing.posted_at)
        self.assertEqual(listing.posted_precision, "unknown")

    def test_mutable_defaults_are_not_shared(self):
        a, b = make(), make()
        a.resume_keywords.append("Unity")
        self.assertEqual(b.resume_keywords, [])

    def test_posted_at_accepts_aware_datetime(self):
        when = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(make(posted_at=when).posted_at, when)


if __name__ == "__main__":
    unittest.main()
