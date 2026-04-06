import os
import unittest
from pathlib import Path
import sys

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.append(str(Path(__file__).resolve().parents[1]))

from storage_helpers import to_int_or_none


class StorageHelpersTests(unittest.TestCase):
    def test_to_int_or_none_extracts_numbers(self):
        self.assertEqual(to_int_or_none("Se 3"), 3)
        self.assertEqual(to_int_or_none("12"), 12)
        self.assertIsNone(to_int_or_none("abc"))
        self.assertIsNone(to_int_or_none(None))


if __name__ == "__main__":
    unittest.main()
