from datetime import datetime
from typing import Optional
import os
import sys

import requests

from storage_helpers import to_int_or_none


def log_stderr(msg: str):
    """Timestamped log to stderr for cloud visibility."""
    ts = datetime.utcnow().isoformat()
    print(f"[{ts}] [STORAGE] {msg}", file=sys.stderr, flush=True)


def _to_int_or_none(val) -> Optional[int]:
    return to_int_or_none(val)


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
        log_stderr("END sync_slots_to_convex success")
        return res

    return {"ok": False, "opened": 0, "updated": 0, "changes": []}


def mark_absent_in_convex(scrape_ts: datetime) -> bool:
    """Ask Convex to mark slots not seen in this scrape as unavailable."""
    log_stderr("START mark_absent_in_convex")
    res = post_to_convex("markAbsent", {"scrape_ts": scrape_ts.isoformat()})
    if res:
        log_stderr("END mark_absent_in_convex success")
        return True
    return False


def fetch_slots_from_convex() -> dict:
    res = post_to_convex("slots/available", {})
    if not res or not res.get("ok"):
        return {"last_scraped_at": None, "items": []}
    return {
        "last_scraped_at": res.get("last_scraped_at"),
        "items": list(res.get("items") or []),
    }


def store_scrape_log(
    opened: int,
    updated: int,
    total: int,
    success: bool,
    message: str = "",
    duration_seconds: float = 0.0,
    pages_scraped: int = 0,
) -> bool:
    """Best-effort write of a scrape summary to Convex."""
    res = post_to_convex("scrape/log", {
        "opened": int(opened or 0),
        "updated": int(updated or 0),
        "total": int(total or 0),
        "success": bool(success),
        "message": message or "",
        "duration_seconds": float(duration_seconds or 0.0),
        "pages_scraped": int(pages_scraped or 0),
    })
    return bool(res and res.get("ok"))
