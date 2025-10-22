# notifications.py
from __future__ import annotations

import os
import httpx
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from storage import _get_supabase_client, log_scrape_result
from zoneinfo import ZoneInfo

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_API_URL = "https://api.resend.com/emails"
MAIL_FROM = os.getenv("MAIL_FROM", "ExamAlert <obvestila@vozniski.si>")
FRONTEND_UNSUB_BASE = os.getenv("FRONTEND_UNSUB_BASE", "https://vozniski.si/unsubscribe")

def _resend_send(to: List[str] | str, subject: str, html: str, text: Optional[str] = None) -> bool:
    if not RESEND_API_KEY:
        return False
    payload = {
        "from": MAIL_FROM,
        "to": to if isinstance(to, list) else [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                RESEND_API_URL,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json=payload,
            )
            r.raise_for_status()
            return True
    except Exception:
        return False

def _fmt_bool_si(b: bool) -> str:
    return "da" if b else "ne"

def _slot_line(it: Dict[str, Any]) -> str:
    # one-line plain text summary per slot
    parts = [
        f"{it['date_str']} ob {it['time_str']}",
        f"{it.get('location') or ''}".strip(),
        f"kat: {it.get('categories') or '-'}",
    ]
    if it.get("exam_type"):
        parts.append(f"Tip izpita: {it['exam_type']}")
    if it.get("tolmac") is not None:
        parts.append(f"tolmač: {_fmt_bool_si(bool(it['tolmac']))}")
    if it.get("places_left") is not None:
        parts.append(f"mesta: {it['places_left']}")
    return " — ".join([p for p in parts if p])

def _render_email(sub: Dict[str, Any], items: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    # subject
    label_loc = sub.get("filter_town") or (f"Območje {int(sub['filter_obmocje'])}" if sub.get("filter_obmocje") is not None else "vsi centri")
    label_cat = sub.get("filter_categories") or "vse kategorije"
    label_tip = sub.get("filter_exam_type") or "teorija/vožnja"
    n = len(items)
    subject = f"Novi termini ({n}) za filter: {label_loc}, {label_cat}, {label_tip}"

    # plain text
    lines = []
    lines.append("Pozdravljeni,")
    lines.append("")
    lines.append("Na voljo so novi termini, ki ustrezajo vašim nastavitvam:")
    crit = []
    if sub.get("filter_obmocje") is not None:
        crit.append(f"Območje {int(sub['filter_obmocje'])}")
    if sub.get("filter_town"):
        crit.append(sub["filter_town"])
    if sub.get("filter_categories"):
        crit.append(f"kategorija {sub['filter_categories']}")
    if sub.get("filter_exam_type"):
        crit.append(f"tip {sub['filter_exam_type']}")
    if sub.get("filter_tolmac"):
        crit.append("tolmač: da")
    if crit:
        lines.append(" • " + " • ".join(crit))
    lines.append("")
    for it in items:
        lines.append(" - " + _slot_line(it))
    lines.append("")
    unsub = sub.get("unsubscribe_token")
    if unsub:
        lines.append(f"Odjava: {FRONTEND_UNSUB_BASE}?token={unsub}")
    text = "\n".join(lines)

    # very simple HTML
    html_lines = []
    html_lines.append("<p>Pozdravljeni,</p>")
    html_lines.append("<p>Na voljo so novi termini, ki ustrezajo vašim nastavitvam:</p>")
    if crit:
        html_lines.append("<p>" + " • ".join(crit) + "</p>")
    html_lines.append("<ul>")
    for it in items:
        html_lines.append(f"<li>{_slot_line(it)}</li>")
    html_lines.append("</ul>")
    if unsub:
        html_lines.append(f'<p><a href="{FRONTEND_UNSUB_BASE}?token={unsub}">Odjava od obvestil</a></p>')
    html = "\n".join(html_lines)

    return subject, text, html

def _fetch_active_subscriptions() -> List[Dict[str, Any]]:
    sb = _get_supabase_client()
    if not sb:
        return []
    try:
        res = sb.table("subscriptions").select("*").eq("active", True).execute()
        # python-supabase returns .data
        return list(res.data or [])
    except Exception:
        return []

def _match(sub: Dict[str, Any], slot: Dict[str, Any]) -> bool:
    # obmocje
    if sub.get("filter_obmocje") is not None:
        try:
            if int(sub["filter_obmocje"]) != int(slot.get("obmocje") or -1):
                return False
        except Exception:
            return False
    # town
    if sub.get("filter_town"):
        if (slot.get("town") or "").strip().lower() != sub["filter_town"].strip().lower():
            return False
    # exam type
    if sub.get("filter_exam_type"):
        if (slot.get("exam_type") or "").strip().lower() != sub["filter_exam_type"].strip().lower():
            return False
    # tolmac == True means require tolmac; False/None -> ignore
    if bool(sub.get("filter_tolmac")):
        if not bool(slot.get("tolmac")):
            return False
    # categories: subscription is a single code; slot may contain CSV
    if sub.get("filter_categories"):
        want = sub["filter_categories"].strip().upper()
        have = {t.strip().upper() for t in (slot.get("categories") or "").split(",") if t.strip()}
        if want and want not in have:
            return False
    return True

def notify_subscribers_for_changes(changes: List[Dict[str, Any]], scrape_ts: datetime) -> int:
    """
    Group newly available slots by subscription, send one email per subscription,
    and update last_notified_at.
    Returns number of subscription emails sent.
    """
    if not changes:
        return 0

    subs = _fetch_active_subscriptions()
    if not subs:
        return 0

    # build matches per subscription id
    by_sub: dict[int, list[Dict[str, Any]]] = {}
    for slot in changes:
        for sub in subs:
            if not sub.get("active", True):
                continue
            if _match(sub, slot):
                by_sub.setdefault(int(sub["id"]), []).append(slot)

    if not by_sub:
        return 0

    sb = _get_supabase_client()
    sent = 0
    for sub in subs:
        sid = int(sub["id"])
        items = by_sub.get(sid)
        if not items:
            continue
        subject, text, html = _render_email(sub, items)
        ok = _resend_send(sub["email"], subject, html, text)
        if ok:
            sent += 1
            # best-effort: update last_notified_at
            try:
                if sb:
                    sb.table("subscriptions") \
                      .update({"last_notified_at": scrape_ts.isoformat()}) \
                      .eq("id", sid) \
                      .execute()
            except Exception:
                pass

    return sent

def send_test_email(scrape_stats: dict, changes: List[Dict[str, Any]]) -> bool:
    """
    Always send a short test mail to gal.gustin@gmail.com with scrape summary.
    """
    to = "gal.gustin@student.um.si"
    n_changes = len(changes)
    subject = f"[Test] Scrape {scrape_stats.get('scrape_ts')}: {scrape_stats.get('total')} fetched, new/reappear {n_changes}"
    lines = ["Scrape summary:",
             f" - total fetched: {scrape_stats.get('total')}",
             f" - opened: {scrape_stats.get('opened')}",
             f" - reappeared: {scrape_stats.get('updated')}",
             f" - changes listed below ({min(n_changes, 10)} shown):",
            ]
    for it in changes[:10]:
        lines.append(" * " + _slot_line(it))
    text = "\n".join(lines)
    html = "<pre>" + "\n".join(lines) + "</pre>"
    return _resend_send(to, subject, html, text)

# --- Daily summary (once per day) ---

def _dt_range_for_local_day(now_utc: datetime, tz: str = "Europe/Ljubljana") -> tuple[str, str, str]:
    """
    Return (day_label, start_iso_utc, end_iso_utc) where start/end bound the local calendar day.
    day_label = 'YYYY-MM-DD' in local time (used for subject + idempotency marker)
    """
    local = now_utc.astimezone(ZoneInfo(tz))
    day_label = local.date().isoformat()

    start_local = datetime(local.year, local.month, local.day, 0, 0, 0, tzinfo=ZoneInfo(tz))
    end_local   = datetime(local.year, local.month, local.day, 23, 59, 59, tzinfo=ZoneInfo(tz))

    start_utc = start_local.astimezone(ZoneInfo("UTC")).isoformat()
    end_utc   = end_local.astimezone(ZoneInfo("UTC")).isoformat()
    return day_label, start_utc, end_utc


def send_daily_summary_if_due(now_utc: datetime) -> bool:
    """
    Sends at most one summary email per local day (Europe/Ljubljana).
    Aggregates today's rows from scrape_logs, emails totals + per-scrape lines,
    then inserts a marker row `message='daily_summary_sent YYYY-MM-DD'`.
    Returns True if an email was sent.
    """
    sb = _get_supabase_client()
    if not sb or not RESEND_API_KEY:
        return False

    day_label, start_iso_utc, end_iso_utc = _dt_range_for_local_day(now_utc, "Europe/Ljubljana")
    marker_msg = f"daily_summary_sent {day_label}"

    try:
        # Idempotency check
        chk = sb.table("scrape_logs").select("id").eq("message", marker_msg).execute()
        if (chk.data or []):
            return False

        # Pull today's logs
        res = sb.table("scrape_logs") \
                .select("timestamp,opened,updated,total,success,message") \
                .gte("timestamp", start_iso_utc) \
                .lte("timestamp", end_iso_utc) \
                .order("timestamp", desc=False) \
                .execute()
        rows = list(res.data or [])
    except Exception:
        return False

    if not rows:
        return False

    # Aggregate
    agg_opened = sum(int(r.get("opened") or 0) for r in rows)
    agg_updated = sum(int(r.get("updated") or 0) for r in rows)
    agg_total = sum(int(r.get("total") or 0) for r in rows)
    n_scrapes = len(rows)

    # Build body
    lines = []
    lines.append(f"Daily scrape summary for {day_label}")
    lines.append("")
    lines.append(f"Scrapes: {n_scrapes}")
    lines.append(f"Opened total: {agg_opened}")
    lines.append(f"Reappeared total: {agg_updated}")
    lines.append(f"Fetched total (sum over scrapes): {agg_total}")
    lines.append("")
    lines.append("Per-scrape timeline (UTC):")
    for r in rows:
        ts = r.get("timestamp")
        ok = "ok" if r.get("success") else "FAIL"
        lines.append(
            f" - {ts}: opened={int(r.get('opened') or 0)}, reappeared={int(r.get('updated') or 0)}, fetched={int(r.get('total') or 0)} [{ok}]"
        )

    text = "\n".join(lines)
    html = "<pre>" + text + "</pre>"
    subject = f"[Daily] Scrape summary {day_label} — {n_scrapes} runs, opened {agg_opened}, reappeared {agg_updated}"

    to = "gal.gustin@student.um.si"
    ok = _resend_send(to, subject, html, text)
    if not ok:
        return False

    # Marker row to prevent duplicate sends the same day
    try:
        log_scrape_result(sb, opened=0, updated=0, total=0, success=True, message=marker_msg)
    except Exception:
        try:
            sb.table("scrape_logs").insert({
                "timestamp": datetime.utcnow().isoformat(),
                "opened": 0, "updated": 0, "total": 0,
                "success": True, "message": marker_msg,
            }).execute()
        except Exception:
            pass

    return True
