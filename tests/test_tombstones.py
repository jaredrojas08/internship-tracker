import json
import tempfile
import unittest
from pathlib import Path

import tombstones


class TestTombstones(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "removed.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_loads_as_empty(self):
        self.assertEqual(tombstones.load(self.path), set())

    def test_add_then_load_round_trips(self):
        tombstones.add(["greenhouse.io:123"], self.path)
        self.assertEqual(tombstones.load(self.path), {"greenhouse.io:123"})

    def test_add_is_idempotent(self):
        tombstones.add(["a"], self.path)
        added = tombstones.add(["a"], self.path)
        self.assertEqual(added, 0)
        self.assertEqual(tombstones.load(self.path), {"a"})

    def test_file_stays_sorted_so_diffs_are_readable(self):
        tombstones.add(["c", "a", "b"], self.path)
        self.assertEqual(json.loads(self.path.read_text()), ["a", "b", "c"])

    def test_corrupt_file_loads_as_empty_rather_than_raising(self):
        self.path.write_text("{not json")
        self.assertEqual(tombstones.load(self.path), set())


if __name__ == "__main__":
    unittest.main()
