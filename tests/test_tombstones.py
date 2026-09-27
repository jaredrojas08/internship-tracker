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

    def test_null_file_loads_as_empty(self):
        self.path.write_text("null")
        self.assertEqual(tombstones.load(self.path), set())

    def test_number_file_loads_as_empty(self):
        self.path.write_text("123")
        self.assertEqual(tombstones.load(self.path), set())

    def test_dict_file_loads_as_empty(self):
        self.path.write_text('{"a": 1}')
        self.assertEqual(tombstones.load(self.path), set())

    def test_string_file_loads_as_empty(self):
        self.path.write_text('"somestring"')
        self.assertEqual(tombstones.load(self.path), set())

    def test_number_list_loads_as_empty(self):
        self.path.write_text("[1, 2, 3]")
        self.assertEqual(tombstones.load(self.path), set())

    def test_mixed_list_filters_non_strings(self):
        self.path.write_text('["ok", 5, null]')
        self.assertEqual(tombstones.load(self.path), {"ok"})

    def test_empty_list_loads_as_empty(self):
        self.path.write_text("[]")
        self.assertEqual(tombstones.load(self.path), set())

    def test_list_with_empty_string_filters_it_out(self):
        self.path.write_text('[""]')
        self.assertEqual(tombstones.load(self.path), set())


if __name__ == "__main__":
    unittest.main()
