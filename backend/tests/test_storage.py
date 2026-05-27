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

    def test_publish_slots_blob_posts_snapshot_to_app_endpoint(self):
        calls = []

        class FakeResponse:
            def raise_for_status(self):
                return None

        original_post = storage.requests.post
        original_env = dict(os.environ)
        storage.requests.post = lambda url, json=None, headers=None, timeout=None, **kwargs: (
            calls.append({"url": url, "json": json, "headers": headers or {}, "timeout": timeout}),
            FakeResponse(),
        )[1]

        try:
            os.environ["APP_BASE_URL"] = "https://vozniski.example"
            os.environ["SCRAPE_SECRET"] = "secret-123"

            ok = storage.publish_slots_blob(
                [
                    {
                        "date_str": "25. 05. 2026",
                        "time_str": "08:00",
                        "obmocje": 2,
                        "town": "Ljubljana",
                        "categories": "B",
                        "places_left": "3",
                    }
                ],
                storage.datetime(2026, 5, 25, 10, 0, 0),
            )
        finally:
            storage.requests.post = original_post
            os.environ.clear()
            os.environ.update(original_env)

        self.assertTrue(ok)
        self.assertEqual(calls[0]["url"], "https://vozniski.example/api/cache/slots/blob")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer secret-123")
        self.assertEqual(calls[0]["json"]["count"], 1)
        self.assertEqual(calls[0]["json"]["last_scraped_at"], "2026-05-25T10:00:00")
        self.assertEqual(calls[0]["json"]["items"][0]["places_left"], 3)
        self.assertEqual(calls[0]["timeout"], 20)

    def test_publish_slots_blob_logs_error_response_body(self):
        logs = []

        class FakeResponse:
            text = '{"ok":false,"detail":"No blob credentials found"}'

            def raise_for_status(self):
                error = storage.requests.exceptions.HTTPError("502 Server Error")
                error.response = self
                raise error

        original_post = storage.requests.post
        original_log = storage.log_stderr
        original_env = dict(os.environ)
        storage.requests.post = lambda *args, **kwargs: FakeResponse()
        storage.log_stderr = logs.append

        try:
            os.environ["APP_BASE_URL"] = "https://vozniski.example"
            os.environ["SCRAPE_SECRET"] = "secret-123"

            ok = storage.publish_slots_blob([], storage.datetime(2026, 5, 25, 10, 0, 0))
        finally:
            storage.requests.post = original_post
            storage.log_stderr = original_log
            os.environ.clear()
            os.environ.update(original_env)

        self.assertFalse(ok)
        self.assertIn("response_body=", logs[0])
        self.assertIn("No blob credentials found", logs[0])

    def test_mark_absent_sends_seen_slot_keys(self):
        calls = []
        original_post_to_convex = storage.post_to_convex
        storage.post_to_convex = lambda action_path, payload, **kwargs: (
            calls.append({"action_path": action_path, "payload": payload}),
            {"ok": True},
        )[1]

        try:
            ok = storage.mark_absent_in_convex(
                storage.datetime(2026, 5, 25, 10, 0, 0),
                [
                    {
                        "date_str": "25. 05. 2026",
                        "time_str": "08:00",
                        "obmocje": 2,
                        "town": "Ljubljana",
                        "categories": "B",
                    }
                ],
            )
        finally:
            storage.post_to_convex = original_post_to_convex

        self.assertTrue(ok)
        self.assertEqual(calls[0]["action_path"], "markAbsent")
        self.assertEqual(calls[0]["payload"]["seenKeys"], ['["25. 05. 2026","08:00",2,"Ljubljana","B"]'])

    def test_prime_slots_cache_uses_current_scrape_without_convex_fetch(self):
        original_post_to_convex = storage.post_to_convex
        storage.post_to_convex = lambda *args, **kwargs: self.fail("fetch_slots_from_convex should use primed cache")

        try:
            storage.prime_slots_cache(
                [
                    {
                        "date_str": "25. 05. 2026",
                        "time_str": "08:00",
                        "obmocje": 2,
                        "town": "Ljubljana",
                        "categories": "B",
                        "places_left": "3",
                    }
                ],
                storage.datetime(2026, 5, 25, 10, 0, 0),
            )
            cached = storage.fetch_slots_from_convex()
        finally:
            storage._clear_slots_cache()
            storage.post_to_convex = original_post_to_convex

        self.assertEqual(cached["last_scraped_at"], "2026-05-25T10:00:00")
        self.assertEqual(cached["items"][0]["town"], "Ljubljana")
        self.assertEqual(cached["items"][0]["places_left"], 3)

    def test_store_scrape_log_sends_city_hits_as_records(self):
        calls = []
        original_post_to_convex = storage.post_to_convex
        storage.post_to_convex = lambda action_path, payload, **kwargs: (
            calls.append({"action_path": action_path, "payload": payload}),
            {"ok": True},
        )[1]

        try:
            ok = storage.store_scrape_log(
                opened=1,
                updated=0,
                total=5,
                success=True,
                notification_stats={"city_hits": {"Domžale": 2}},
            )
        finally:
            storage.post_to_convex = original_post_to_convex

        self.assertTrue(ok)
        self.assertEqual(calls[0]["action_path"], "scrape/log")
        self.assertEqual(calls[0]["payload"]["notification_city_hits"], [{"city": "Domžale", "count": 2}])


if __name__ == "__main__":
    unittest.main()
