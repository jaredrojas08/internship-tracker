import unittest
from datetime import datetime, timezone

import parser as md
import sources


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

    def test_job_id_reads_gh_jid_when_the_path_has_no_id(self):
        # Nintendo links every posting as careers.nintendo.com/?gh_jid=<id>.
        a = make(apply_url="https://careers.nintendo.com/?gh_jid=4421632009")
        b = make(apply_url="https://careers.nintendo.com/?gh_jid=4471972009")
        self.assertNotEqual(a.job_id, b.job_id)
        self.assertEqual(a.job_id, "careers.nintendo.com:4421632009")

    def test_job_id_uses_the_whole_uuid_on_ashby_and_lever(self):
        # A six-digit run inside a UUID is not an id; Notion's Frontend posting
        # came out as jobs.ashbyhq.com:803253 and could collide across boards.
        a = make(apply_url="https://jobs.ashbyhq.com/notion/43ffba97-b64a-4c6a-ac13-c803253c9413")
        b = make(apply_url="https://jobs.ashbyhq.com/other/11111111-2222-4333-8444-c803253c9413")
        self.assertEqual(a.job_id, "jobs.ashbyhq.com:43ffba97-b64a-4c6a-ac13-c803253c9413")
        self.assertNotEqual(a.job_id, b.job_id)
        lever = make(apply_url="https://jobs.lever.co/acme/0C6E9A2B-1D4F-4E5A-9B7C-123456789ABC/apply")
        self.assertEqual(lever.job_id, "jobs.lever.co:0c6e9a2b-1d4f-4e5a-9b7c-123456789abc")

    def test_job_id_is_a_plain_string(self):
        self.assertIsInstance(make().job_id, str)
        self.assertTrue(make().job_id)

    def test_job_id_differs_across_companies_when_url_is_empty(self):
        a = make(company="Riot Games", role="Software Engineer Intern", apply_url="")
        b = make(company="Valve", role="Software Engineer Intern", apply_url="")
        self.assertNotEqual(a.job_id, b.job_id)

    def test_job_id_matches_across_runs_when_url_is_empty(self):
        a = make(company="Riot Games", role="Software Engineer Intern", apply_url="")
        b = make(company="Riot Games", role="Software Engineer Intern", apply_url="")
        self.assertEqual(a.job_id, b.job_id)


class TestDeduplicate(unittest.TestCase):
    def test_same_title_on_one_board_with_different_ids_is_kept(self):
        # Sony posts "Software Engineering Intern" once per team and city; title
        # matching dropped San Diego because San Mateo came first.
        san_mateo = make(company="Sony Interactive Entertainment", location="San Mateo, CA",
                         apply_url="https://job-boards.greenhouse.io/sie/jobs/6221793004")
        san_diego = make(company="Sony Interactive Entertainment", location="San Diego, CA",
                         apply_url="https://job-boards.greenhouse.io/sie/jobs/6221754004")
        unique, duplicates = sources.deduplicate([san_mateo, san_diego])
        self.assertEqual(unique, [san_mateo, san_diego])
        self.assertEqual(duplicates, 0)

    def test_same_title_from_another_site_is_still_merged(self):
        board = make(apply_url="https://job-boards.greenhouse.io/riotgames/jobs/1234567")
        aggregator = make(apply_url="https://jobright.ai/jobs/info/abcdef1234567890", source="jobright")
        unique, duplicates = sources.deduplicate([board, aggregator])
        self.assertEqual(unique, [board])
        self.assertEqual(duplicates, 1)

    def test_same_posting_from_two_lists_is_merged(self):
        a = make(apply_url="https://boards.greenhouse.io/riotgames/jobs/1234567")
        b = make(apply_url="https://job-boards.greenhouse.io/riotgames/jobs/1234567?utm=x")
        self.assertEqual(sources.deduplicate([a, b]), ([a], 1))


class TestTerm(unittest.TestCase):
    def test_role_naming_a_term_carries_it(self):
        listing = make(role="Winter 2027 Co-op")
        self.assertEqual(listing.term, "Winter 2027")

    def test_role_with_no_term_is_unspecified(self):
        self.assertEqual(make(role="Software Engineer Intern").term, "Unspecified")


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
