from datetime import datetime
from typing import Any, Dict, List, Optional


def parse_slot_date(slot: Dict[str, Any]) -> Optional[datetime]:
    try:
        return datetime.strptime(str(slot.get("date_str") or "").strip(), "%d. %m. %Y")
    except Exception:
        return None


def slot_within_notification_window(
    slot: Dict[str, Any],
    scrape_ts: datetime,
    max_days: int,
) -> bool:
    slot_dt = parse_slot_date(slot)
    if slot_dt is None:
        return False
    delta_days = (slot_dt.date() - scrape_ts.date()).days
    return 0 <= delta_days <= max_days


def subscription_order_key(sub: Dict[str, Any]) -> tuple[str, str]:
    return (str(sub.get("created_at") or ""), str(sub.get("id") or ""))


def canonical_subscriptions(subs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    latest_by_email: Dict[str, Dict[str, Any]] = {}
    for sub in subs:
        email = str(sub.get("email") or "").strip().lower()
        if not email:
            continue
        candidate_key = subscription_order_key(sub)
        current = latest_by_email.get(email)
        current_key = subscription_order_key(current) if current else ("", "")
        if current is None or candidate_key >= current_key:
            latest_by_email[email] = sub
    return list(latest_by_email.values())
