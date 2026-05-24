import unittest
from datetime import datetime
from pathlib import Path
import sys
import types

sys.path.append(str(Path(__file__).resolve().parents[1]))
sys.modules.setdefault("httpx", types.SimpleNamespace(Client=None))

from notification_policy import (
    canonical_subscriptions,
    slot_within_notification_window,
)
import notifications
from notifications import _aggregate_city_hits, _render_email, notify_subscribers_for_changes, send_daily_summary_if_due


class NotificationPolicyTests(unittest.TestCase):
    def test_keeps_latest_subscription_per_email(self):
        subs = [
            {"id": 1, "email": "user@example.com", "created_at": "2026-03-01T10:00:00"},
            {"id": 2, "email": "USER@example.com", "created_at": "2026-03-02T10:00:00"},
            {"id": 3, "email": "second@example.com", "created_at": "2026-03-01T10:00:00"},
        ]

        canonical = canonical_subscriptions(subs)
        by_email = {item["email"].lower(): item["id"] for item in canonical}

        self.assertEqual(by_email["user@example.com"], 2)
        self.assertEqual(by_email["second@example.com"], 3)
        self.assertEqual(len(canonical), 2)

    def test_keeps_latest_subscription_with_convex_string_ids(self):
        subs = [
            {"id": "jx111", "email": "user@example.com", "created_at": "2026-03-01T10:00:00"},
            {"id": "jx222", "email": "USER@example.com", "created_at": "2026-03-02T10:00:00"},
        ]

        canonical = canonical_subscriptions(subs)

        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0]["id"], "jx222")

    def test_notification_window_blocks_far_future_slots(self):
        scrape_ts = datetime(2026, 3, 15, 9, 0, 0)

        self.assertTrue(
            slot_within_notification_window(
                {"date_str": "09. 04. 2026"},
                scrape_ts,
                max_days=25,
            )
        )
        self.assertFalse(
            slot_within_notification_window(
                {"date_str": "10. 04. 2026"},
                scrape_ts,
                max_days=25,
            )
        )

    def test_renders_notification_in_subscription_language(self):
        slot = {
            "date_str": "09. 04. 2026",
            "time_str": "08:00",
            "location": "Ljubljana",
            "categories": "B",
            "exam_type": "voznja",
            "places_left": 2,
        }

        en_subject, en_text, en_html = _render_email(
            {"filter_town": "Ljubljana", "language": "en", "unsubscribe_token": "abc"},
            [slot],
        )
        sl_subject, sl_text, sl_html = _render_email(
            {"filter_town": "Ljubljana", "language": "sl", "unsubscribe_token": "abc"},
            [slot],
        )

        self.assertIn("New exam slots", en_subject)
        self.assertIn("Hello", en_text)
        self.assertIn('lang="en"', en_html)
        self.assertIn("Novi termini", sl_subject)
        self.assertIn("Pozdravljeni", sl_text)
        self.assertIn('lang="sl"', sl_html)

    def test_notifies_subscription_with_convex_string_id(self):
        calls = []

        def fake_post_to_convex(action_path, payload):
            calls.append((action_path, payload))
            if action_path == "notifications/subscriptions":
                return {
                    "ok": True,
                    "subscriptions": [
                        {
                            "id": "jx222",
                            "email": "user@example.com",
                            "active": True,
                            "created_at": "2026-03-02T10:00:00",
                            "filter_town": "Ljubljana",
                            "filter_exam_type": "voznja",
                            "filter_categories": "B",
                            "unsubscribe_token": "abc",
                        }
                    ],
                }
            if action_path == "notifications/subscription/notified":
                return {"ok": True}
            return None

        original_post_to_convex = notifications.post_to_convex
        original_resend_send = notifications._resend_send
        notifications.post_to_convex = fake_post_to_convex
        notifications._resend_send = lambda to, subject, html, text=None: to == "user@example.com"
        try:
            stats = notify_subscribers_for_changes(
                [
                    {
                        "date_str": "09. 04. 2026",
                        "time_str": "08:00",
                        "location": "Ljubljana",
                        "town": "Ljubljana",
                        "categories": "B",
                        "exam_type": "voznja",
                        "places_left": 2,
                    }
                ],
                datetime(2026, 3, 15, 9, 0, 0),
            )
        finally:
            notifications.post_to_convex = original_post_to_convex
            notifications._resend_send = original_resend_send

        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["matching_accounts"], 1)
        self.assertEqual(stats["matched_pairs"], 1)
        self.assertEqual(stats["matched_slots"], 1)
        self.assertEqual(stats["city_hits"], {"Ljubljana": 1})
        self.assertIn(
            ("notifications/subscription/notified", {"id": "jx222", "last_notified_at": "2026-03-15T09:00:00"}),
            calls,
        )

    def test_aggregates_top_20_city_hits(self):
        rows = [
            {
                "notification_city_hits": {
                    f"City {index:02d}": index
                    for index in range(1, 22)
                }
            }
        ]

        top_cities = _aggregate_city_hits(rows, limit=20)

        self.assertEqual(len(top_cities), 20)
        self.assertEqual(top_cities[0], ("City 21", 21))
        self.assertNotIn(("City 01", 1), top_cities)

    def test_daily_summary_includes_notification_and_activity_stats(self):
        sent_messages = []

        def fake_post_to_convex(action_path, payload):
            if action_path == "scrape/log/marker":
                return {"ok": True, "exists": False}
            if action_path == "scrape/logs/range":
                return {
                    "ok": True,
                    "logs": [
                        {
                            "timestamp": "2026-03-15T08:00:00",
                            "opened": 2,
                            "updated": 1,
                            "total": 50,
                            "success": True,
                            "notification_sent": 3,
                            "notification_failed": 1,
                            "notification_matching_accounts": 4,
                            "notification_matched_pairs": 7,
                            "notification_matched_slots": 5,
                            "notification_out_of_window": 2,
                            "notification_city_hits": {"Ljubljana": 3, "Maribor": 1},
                        }
                    ],
                }
            if action_path == "activity/stats/range":
                return {
                    "ok": True,
                    "stats": {
                        "new_users": 2,
                        "active_users": 9,
                        "total_users": 10,
                        "new_subscriptions": 3,
                        "active_subscriptions": 8,
                    },
                }
            if action_path == "scrape/log":
                return {"ok": True}
            return None

        original_key = notifications.RESEND_API_KEY
        original_post_to_convex = notifications.post_to_convex
        original_resend_send = notifications._resend_send
        original_store_scrape_log = notifications.store_scrape_log
        notifications.RESEND_API_KEY = "test"
        notifications.post_to_convex = fake_post_to_convex
        notifications._resend_send = lambda to, subject, html, text=None: sent_messages.append((subject, text)) or True
        notifications.store_scrape_log = lambda *args, **kwargs: True
        try:
            ok = send_daily_summary_if_due(datetime(2026, 3, 15, 20, 0, 0))
        finally:
            notifications.RESEND_API_KEY = original_key
            notifications.post_to_convex = original_post_to_convex
            notifications._resend_send = original_resend_send
            notifications.store_scrape_log = original_store_scrape_log

        self.assertTrue(ok)
        subject, text = sent_messages[0]
        self.assertIn("notified 3", subject)
        self.assertIn("Accounts emailed: 3", text)
        self.assertIn("Accounts with filter hits: 4", text)
        self.assertIn("Ljubljana: 3", text)
        self.assertIn("New users today: 2", text)
        self.assertIn("Active subscriptions: 8", text)


if __name__ == "__main__":
    unittest.main()
