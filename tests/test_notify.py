import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import notify


class TestDigestCadence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "digest_state.json"
        self.now = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.dir.cleanup()

    def test_first_ever_run_is_due(self):
        self.assertTrue(notify.digest_is_due(self.path, self.now))

    def test_not_due_an_hour_after_sending(self):
        self.path.write_text(json.dumps({"last_sent": (self.now - timedelta(hours=1)).isoformat()}))
        self.assertFalse(notify.digest_is_due(self.path, self.now))

    def test_due_again_after_24_hours(self):
        self.path.write_text(json.dumps({"last_sent": (self.now - timedelta(hours=25)).isoformat()}))
        self.assertTrue(notify.digest_is_due(self.path, self.now))

    def test_corrupt_state_is_treated_as_due(self):
        self.path.write_text("{not json")
        self.assertTrue(notify.digest_is_due(self.path, self.now))


if __name__ == "__main__":
    unittest.main()
