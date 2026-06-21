import json
import re
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


def time_to_minutes(value: Any) -> Optional[int]:
    match = re.match(r"^(\d{1,2}):(\d{2})", str(value or "").strip())
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def parse_time_windows(value: Any) -> List[Dict[str, str]]:
    if not value:
        return []
    try:
        windows = json.loads(value) if isinstance(value, str) else value
    except Exception:
        return []
    if not isinstance(windows, list):
        return []

    normalized: List[Dict[str, str]] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        start = str(window.get("start") or "").strip()
        end = str(window.get("end") or "").strip()
        start_minutes = time_to_minutes(start)
        end_minutes = time_to_minutes(end)
        if start_minutes is None or end_minutes is None or start_minutes >= end_minutes:
            continue
        normalized.append({"start": start[:5], "end": end[:5]})
        if len(normalized) == 2:
            break
    return normalized


def slot_within_time_windows(slot: Dict[str, Any], windows_value: Any) -> bool:
    windows = parse_time_windows(windows_value)
    if not windows:
        return True
    slot_minutes = time_to_minutes(slot.get("time_iso") or slot.get("time_str"))
    if slot_minutes is None:
        return False
    for window in windows:
        start = time_to_minutes(window["start"])
        end = time_to_minutes(window["end"])
        if start is not None and end is not None and start <= slot_minutes <= end:
            return True
    return False


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
