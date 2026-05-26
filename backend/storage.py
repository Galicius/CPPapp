from datetime import UTC, datetime
from typing import Optional
import os
import json
import sys
import time

import requests

from storage_helpers import to_int_or_none

SLOTS_CACHE_TTL_SECONDS = int(os.getenv("SLOTS_CACHE_TTL_SECONDS", "60"))
_slots_cache: dict | None = None
_slots_cache_until = 0.0


def log_stderr(msg: str):
    """Timestamped log to stderr for cloud visibility."""
    ts = datetime.now(UTC).isoformat()
    print(f"[{ts}] [STORAGE] {msg}", file=sys.stderr, flush=True)


def _to_int_or_none(val) -> Optional[int]:
    return to_int_or_none(val)


def _clear_slots_cache():
    global _slots_cache, _slots_cache_until
    _slots_cache = None
    _slots_cache_until = 0.0


def prime_slots_cache(items: list[dict], scrape_ts: datetime):
    global _slots_cache, _slots_cache_until
    _slots_cache = {
        "last_scraped_at": scrape_ts.isoformat(),
        "items": _slot_payload(items),
    }
    _slots_cache_until = time.monotonic() + SLOTS_CACHE_TTL_SECONDS


def _get_convex_url(action_path: str) -> Optional[str]:
    site_url = os.getenv("CONVEX_SITE_URL")
    if not site_url:
        return None
    return f"{site_url.rstrip('/')}/{action_path}"


def _get_convex_headers() -> dict:
    secret = os.getenv("CONVEX_SCRAPER_SECRET")
    if not secret:
        log_stderr("Missing CONVEX_SCRAPER_SECRET")
        return {}
    return {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
    }


def _get_app_url() -> Optional[str]:
    app_url = (
        os.getenv("APP_BASE_URL")
        or os.getenv("NEXT_PUBLIC_APP_URL")
        or os.getenv("VERCEL_PROJECT_PRODUCTION_URL")
        or os.getenv("VERCEL_URL")
    )
    if not app_url:
        origins = [o.strip() for o in os.getenv("FRONTEND_ORIGINS", "").split(",") if o.strip()]
        app_url = origins[0] if origins else "https://examalert.vercel.app"
    if app_url and not app_url.startswith(("http://", "https://")):
        app_url = f"https://{app_url}"
    return app_url.rstrip("/") if app_url else None


def post_to_convex(action_path: str, payload: dict, timeout: int = 10) -> Optional[dict]:
    url = _get_convex_url(action_path)
    headers = _get_convex_headers()

    if not url or not headers.get("Authorization"):
        log_stderr(f"Convex env not set; skipping {action_path}")
        return None

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        response.raise_for_status()
        try:
            return response.json()
        except Exception:
            return {"ok": True}
    except Exception as e:
        log_stderr(f"Convex request failed for {action_path}: {e}")
        return None


def revalidate_slots_cache() -> bool:
    app_url = _get_app_url()
    secret = os.getenv("SCRAPE_SECRET") or os.getenv("SCRAPER_SECRET")
    if not app_url or not secret:
        log_stderr("App URL or SCRAPE_SECRET not set; skipping slots cache revalidation")
        return False

    try:
        response = requests.post(
            f"{app_url}/api/cache/slots/revalidate",
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Secret": secret,
                "Accept": "application/json",
            },
            timeout=10,
        )
        response.raise_for_status()
        log_stderr("Revalidated Vercel slots cache")
        return True
    except Exception as e:
        log_stderr(f"Slots cache revalidation failed: {e}")
        return False


def _normalize_dt_fields(it: dict):
    out = dict(it)
    try:
        out["date_iso"] = datetime.strptime(it["date_str"].strip(), "%d. %m. %Y").date()
    except Exception:
        out["date_iso"] = None
    try:
        out["time_iso"] = datetime.strptime((it["time_str"] or "00:00").strip(), "%H:%M").time()
    except Exception:
        out["time_iso"] = None
    return out


def _slot_payload(items: list[dict]) -> list[dict]:
    rows = []
    for it in items:
        rec = _normalize_dt_fields(it)
        rows.append({
            "date_str": rec["date_str"],
            "time_str": rec["time_str"],
            "date_iso": rec["date_iso"].isoformat() if rec.get("date_iso") else None,
            "time_iso": rec["time_iso"].isoformat() if rec.get("time_iso") else None,
            "obmocje": rec.get("obmocje"),
            "town": rec.get("town"),
            "exam_type": rec.get("exam_type"),
            "places_left": _to_int_or_none(rec.get("places_left")),
            "tolmac": bool(rec.get("tolmac")),
            "categories": rec.get("categories", "") or "",
            "source_page": rec.get("source_page"),
            "location": rec.get("location"),
        })
    return rows


def _slot_key(item: dict) -> str:
    return json.dumps([
        item.get("date_str"),
        item.get("time_str"),
        item.get("obmocje"),
        item.get("town"),
        item.get("categories", "") or "",
    ], separators=(",", ":"))


def _city_hits_payload(city_hits: dict | None) -> list[dict]:
    if not isinstance(city_hits, dict):
        return []
    rows = []
    for city, count in city_hits.items():
        try:
            numeric_count = int(count or 0)
        except (TypeError, ValueError):
            continue
        rows.append({
            "city": str(city or "Unknown").strip() or "Unknown",
            "count": numeric_count,
        })
    return rows


def sync_slots_to_convex(items: list[dict], scrape_ts: datetime) -> dict:
    """Push newly scraped slots to Convex and return Convex change stats."""
    log_stderr(f"START sync_slots_to_convex items={len(items)}")
    if not items:
        return {"ok": True, "opened": 0, "updated": 0, "changes": []}

    res = post_to_convex(
        "syncSlots",
        {"items": _slot_payload(items), "scrape_ts": scrape_ts.isoformat()},
        timeout=20,
    )
    if res:
        _clear_slots_cache()
        log_stderr("END sync_slots_to_convex success")
        return res

    return {"ok": False, "opened": 0, "updated": 0, "changes": []}


def mark_absent_in_convex(scrape_ts: datetime, items: Optional[list[dict]] = None) -> bool:
    """Ask Convex to mark slots not seen in this scrape as unavailable."""
    log_stderr("START mark_absent_in_convex")
    payload = {"scrape_ts": scrape_ts.isoformat()}
    if items is not None:
        payload["seenKeys"] = [_slot_key(item) for item in _slot_payload(items)]

    res = post_to_convex("markAbsent", payload)
    if res:
        _clear_slots_cache()
        log_stderr("END mark_absent_in_convex success")
        return True
    return False


def fetch_slots_from_convex() -> dict:
    global _slots_cache, _slots_cache_until

    now = time.monotonic()
    if _slots_cache is not None and now < _slots_cache_until:
        return _slots_cache

    res = post_to_convex("slots/available", {})
    if not res or not res.get("ok"):
        return _slots_cache or {"last_scraped_at": None, "items": []}

    _slots_cache = {
        "last_scraped_at": res.get("last_scraped_at"),
        "items": list(res.get("items") or []),
    }
    _slots_cache_until = now + SLOTS_CACHE_TTL_SECONDS
    return _slots_cache


def store_scrape_log(
    opened: int,
    updated: int,
    total: int,
    success: bool,
    message: str = "",
    duration_seconds: float = 0.0,
    pages_scraped: int = 0,
    notification_stats: Optional[dict] = None,
) -> bool:
    """Best-effort write of a scrape summary to Convex."""
    payload = {
        "opened": int(opened or 0),
        "updated": int(updated or 0),
        "total": int(total or 0),
        "success": bool(success),
        "message": message or "",
        "duration_seconds": float(duration_seconds or 0.0),
        "pages_scraped": int(pages_scraped or 0),
    }
    if notification_stats:
        payload.update({
            "notification_sent": int(notification_stats.get("sent") or 0),
            "notification_failed": int(notification_stats.get("failed") or 0),
            "notification_matching_accounts": int(notification_stats.get("matching_accounts") or 0),
            "notification_matched_pairs": int(notification_stats.get("matched_pairs") or 0),
            "notification_matched_slots": int(notification_stats.get("matched_slots") or 0),
            "notification_out_of_window": int(notification_stats.get("out_of_window") or 0),
            "notification_city_hits": _city_hits_payload(notification_stats.get("city_hits")),
        })

    res = post_to_convex("scrape/log", payload)
    return bool(res and res.get("ok"))
