import unittest
from pathlib import Path
import sys
import os

sys.path.append(str(Path(__file__).resolve().parents[1]))

import storage
from storage_helpers import to_int_or_none


class StorageHelpersTests(unittest.TestCase):
    def test_to_int_or_none_extracts_numbers(self):
        self.assertEqual(to_int_or_none("Se 3"), 3)
        self.assertEqual(to_int_or_none("12"), 12)
        self.assertIsNone(to_int_or_none("abc"))
        self.assertIsNone(to_int_or_none(None))

    def test_revalidate_slots_cache_posts_to_protected_app_endpoint(self):
        calls = []

        class FakeResponse:
            def raise_for_status(self):
                return None

        original_post = storage.requests.post
        original_env = dict(os.environ)
        storage.requests.post = lambda url, headers=None, timeout=None, **kwargs: (
            calls.append({"url": url, "headers": headers or {}, "timeout": timeout}),
            FakeResponse(),
        )[1]

        try:
            os.environ["APP_BASE_URL"] = "https://vozniski.example"
            os.environ["SCRAPE_SECRET"] = "secret-123"

            self.assertTrue(storage.revalidate_slots_cache())
        finally:
            storage.requests.post = original_post
            os.environ.clear()
            os.environ.update(original_env)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], "https://vozniski.example/api/cache/slots/revalidate")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer secret-123")
        self.assertEqual(calls[0]["headers"]["X-Secret"], "secret-123")
        self.assertEqual(calls[0]["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
