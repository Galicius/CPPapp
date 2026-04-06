import unittest
from datetime import datetime
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from notification_policy import (
    canonical_subscriptions,
    slot_within_notification_window,
)


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


if __name__ == "__main__":
    unittest.main()
