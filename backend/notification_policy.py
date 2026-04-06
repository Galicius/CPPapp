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


def canonical_subscriptions(subs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    latest_by_email: Dict[str, Dict[str, Any]] = {}
    for sub in subs:
        email = str(sub.get("email") or "").strip().lower()
        if not email:
            continue
        candidate_key = (str(sub.get("created_at") or ""), int(sub.get("id") or 0))
        current = latest_by_email.get(email)
        current_key = (str(current.get("created_at") or ""), int(current.get("id") or 0)) if current else ("", -1)
        if current is None or candidate_key >= current_key:
            latest_by_email[email] = sub
    return list(latest_by_email.values())
