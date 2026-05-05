# notifications.py
from __future__ import annotations

import os
import sys
from html import escape
import httpx
from datetime import UTC, datetime
from typing import List, Dict, Any, Optional, Tuple
from notification_policy import canonical_subscriptions, slot_within_notification_window
from storage import post_to_convex, store_scrape_log
from zoneinfo import ZoneInfo

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_API_URL = "https://api.resend.com/emails"
MAIL_FROM = os.getenv("MAIL_FROM", "ExamAlert <obvestila@vozniski.si>")
FRONTEND_UNSUB_BASE = os.getenv("FRONTEND_UNSUB_BASE", "https://vozniski.si/api/unsubscribe")
NOTIFICATION_WINDOW_DAYS = int(os.getenv("NOTIFICATION_WINDOW_DAYS", "25"))


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] [NOTIFICATIONS] {msg}", file=sys.stderr, flush=True)


def _resend_send(to: List[str] | str, subject: str, html: str, text: Optional[str] = None) -> bool:
    if not RESEND_API_KEY:
        _log("RESEND_API_KEY missing; skipping email send")
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
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        body = getattr(getattr(exc, "response", None), "text", "")
        _log(f"Resend send failed status={status} error={exc} body={body[:500]}")
        return False

# Styles
FONT_MAIN = "font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;"
BG_DARK = "#020617"     # slate-950
BG_CARD = "#1e293b"     # slate-800
TEXT_WHITE = "#ffffff"
TEXT_GRAY = "#94a3b8"   # slate-400
ACCENT = "#3b82f6"      # blue-500
BORDER = "#334155"      # slate-700

SUPPORTED_LANGUAGES = {"sl", "en"}

EMAIL_COPY = {
    "sl": {
        "unknown_area": "Vsa obmocja",
        "area": "Obmocje",
        "all_categories": "Vse kategorije",
        "all_types": "Vsi tipi",
        "subject": "Novi termini ({n}) - {location}",
        "greeting": "Pozdravljeni,",
        "found": "Nasli smo {n} novih terminov za vase kriterije:",
        "at": "ob",
        "unknown_location": "Neznano",
        "category": "kat",
        "type": "Tip",
        "places": "mesta",
        "free_places": "prostih mest",
        "headline": "Hitro se prijavi!",
        "intro": "Nasli smo <strong style=\"color: {text_white}\">{n}</strong> novih terminov, ki ustrezajo vasim zeljam:",
        "footer": "To sporocilo ste prejeli, ker ste naroceni na obvestila na Vozniski.si.",
        "unsubscribe": "Odjava od obvestil",
        "unsubscribe_text": "Odjava",
        "html_lang": "sl",
    },
    "en": {
        "unknown_area": "All regions",
        "area": "Region",
        "all_categories": "All categories",
        "all_types": "All types",
        "subject": "New exam slots ({n}) - {location}",
        "greeting": "Hello,",
        "found": "We found {n} new slots matching your criteria:",
        "at": "at",
        "unknown_location": "Unknown",
        "category": "cat",
        "type": "Type",
        "places": "places",
        "free_places": "free places",
        "headline": "Book quickly!",
        "intro": "We found <strong style=\"color: {text_white}\">{n}</strong> new slots matching your preferences:",
        "footer": "You received this message because you subscribed to notifications on Vozniski.si.",
        "unsubscribe": "Unsubscribe from notifications",
        "unsubscribe_text": "Unsubscribe",
        "html_lang": "en",
    },
}

def _lang(value: Any) -> str:
    return value if value in SUPPORTED_LANGUAGES else "sl"

def _slot_text_line(it: Dict[str, Any], lang: str = "sl") -> str:
    c = EMAIL_COPY[_lang(lang)]
    # Keeps plain text version simple
    parts = [
        f"{it['date_str']} {c['at']} {it['time_str']}",
        f"{it.get('location') or ''}".strip(),
        f"{c['category']}: {it.get('categories') or '-'}",
    ]
    if it.get("exam_type"):
        parts.append(f"{c['type']}: {it['exam_type']}")
    if it.get("places_left") is not None:
        parts.append(f"{c['places']}: {it['places_left']}")
    return " | ".join([p for p in parts if p])

def _render_slots_html(items: List[Dict[str, Any]], lang: str = "sl") -> str:
    c = EMAIL_COPY[_lang(lang)]
    rows = []
    for it in items:
        # Data preparation
        date_time = f"{escape(str(it['date_str']))} <span style='color: {TEXT_GRAY}; font-weight: normal;'>{c['at']}</span> {escape(str(it['time_str']))}"
        loc = escape(str(it.get('location') or c["unknown_location"]))
        cats = escape(str(it.get('categories') or "-"))
        exam_type = escape(str(it.get('exam_type') or ""))
        places = it.get('places_left')
        
        meta_parts = []
        meta_parts.append(f"<span style='color: {ACCENT}; font-weight: bold;'>{cats}</span>")
        if exam_type:
            meta_parts.append(f"<span>{exam_type.capitalize()}</span>")
        if places is not None:
            meta_parts.append(f"<span>{escape(str(places))} {c['free_places']}</span>")
        
        meta_html = " &bull; ".join(meta_parts)

        row = f"""
        <tr>
            <td style="padding-bottom: 12px;">
                <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: {BG_CARD}; border-radius: 8px; border: 1px solid {BORDER};">
                    <tr>
                        <td style="padding: 16px;">
                            <p style="margin: 0 0 4px 0; font-size: 16px; font-weight: bold; color: {TEXT_WHITE};">
                                {date_time}
                            </p>
                            <p style="margin: 0 0 8px 0; font-size: 14px; color: {TEXT_GRAY};">
                                {loc}
                            </p>
                            <p style="margin: 0; font-size: 13px; color: {TEXT_GRAY};">
                                {meta_html}
                            </p>
                        </td>
                        <td align="right" style="padding: 16px; width: 40px;">
                             <!-- Arrow icon or similar indicator could go here, keeping it clean for now -->
                             <span style="font-size: 20px; color: {ACCENT};">&rarr;</span>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
        """
        rows.append(row)
    return "\n".join(rows)

def _render_email(sub: Dict[str, Any], items: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    lang = _lang(sub.get("language"))
    c = EMAIL_COPY[lang]
    label_loc = sub.get("filter_town") or (f"{c['area']} {int(sub['filter_obmocje'])}" if sub.get("filter_obmocje") is not None else c["unknown_area"])
    label_cat = sub.get("filter_categories") or c["all_categories"]
    n = len(items)
    subject = c["subject"].format(n=n, location=label_loc)

    text_lines = [c["greeting"], "", c["found"].format(n=n), ""]
    for it in items:
        text_lines.append(f" - {_slot_text_line(it, lang)}")
    text_lines.append("")
    unsub_token = sub.get("unsubscribe_token")
    if unsub_token:
        text_lines.append(f"{c['unsubscribe_text']}: {FRONTEND_UNSUB_BASE}?token={unsub_token}")
    text_lines.append("")
    text = "\n".join(text_lines)

    slots_html = _render_slots_html(items, lang)
    intro = c["intro"].format(n=n, text_white=TEXT_WHITE)
    unsubscribe_html = (
        f'<p style="font-size: 12px; margin: 0;"><a href="{FRONTEND_UNSUB_BASE}?token={unsub_token}" style="color: {TEXT_GRAY}; text-decoration: underline;">{c["unsubscribe"]}</a></p>'
        if unsub_token
        else ""
    )

    html = f"""
    <!DOCTYPE html>
    <html lang="{c['html_lang']}">
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escape(subject)}</title>
    </head>
    <body style="{FONT_MAIN} margin: 0; padding: 0; background-color: {BG_DARK}; color: {TEXT_WHITE};">
        <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: {BG_DARK}; width: 100%;">
            <tr>
                <td align="center" style="padding: 40px 10px;">
                    <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; width: 100%;">
                        <tr>
                            <td align="center" style="padding-bottom: 40px;">
                                <h1 style="margin: 0; font-size: 28px; font-weight: 800; color: {TEXT_WHITE}; letter-spacing: -0.5px;">Vozniski.si</h1>
                            </td>
                        </tr>
                        <tr>
                            <td style="padding-bottom: 30px; text-align: center;">
                                <h2 style="margin: 0 0 10px 0; font-size: 24px; font-weight: bold; color: {TEXT_WHITE};">{c['headline']}</h2>
                                <p style="margin: 0; font-size: 16px; line-height: 1.5; color: {TEXT_GRAY};">{intro}</p>
                                <p style="margin: 8px 0 0 0; font-size: 14px; font-weight: 500; color: {ACCENT}; text-transform: uppercase; letter-spacing: 0.5px;">
                                    {escape(str(label_loc))} &bull; {escape(str(label_cat))}
                                </p>
                            </td>
                        </tr>
                        {slots_html}
                        <tr>
                            <td style="border-top: 1px solid {BORDER}; padding-top: 20px; text-align: center;">
                                <p style="font-size: 12px; color: {TEXT_GRAY}; margin: 0 0 10px 0;">{c['footer']}</p>
                                {unsubscribe_html}
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """
    return subject, text, html

    # Labels for context
    label_loc = sub.get("filter_town") or (f"Območje {int(sub['filter_obmocje'])}" if sub.get("filter_obmocje") is not None else "Vsa območja")
    label_cat = sub.get("filter_categories") or "Vse kategorije"
    label_tip = sub.get("filter_exam_type") or "Vsi tipi"
    
    n = len(items)
    subject = f"Novi termini ({n}) - {label_loc}"

    # Plain text fallback
    text_lines = ["Pozdravljeni,", "", f"Našli smo {n} novih terminov za vaše kriterije:", ""]
    for it in items:
        text_lines.append(f" - {_slot_text_line(it)}")
    text_lines.append("")
    unsub_token = sub.get("unsubscribe_token")
    if unsub_token:
        text_lines.append(f"Odjava: {FRONTEND_UNSUB_BASE}?token={unsub_token}")
    text_lines.append("")
    text = "\n".join(text_lines)

    # HTML Email
    slots_html = _render_slots_html(items)
    
    html = f"""
    <!DOCTYPE html>
    <html lang="sl">
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{subject}</title>
    </head>
    <body style="{FONT_MAIN} margin: 0; padding: 0; background-color: {BG_DARK}; color: {TEXT_WHITE};">
        <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: {BG_DARK}; width: 100%;">
            <tr>
                <td align="center" style="padding: 40px 10px;">
                    <!-- Container -->
                    <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; width: 100%;">
                        <!-- Header -->
                        <tr>
                            <td align="center" style="padding-bottom: 40px;">
                                <h1 style="margin: 0; font-size: 28px; font-weight: 800; color: {TEXT_WHITE}; letter-spacing: -0.5px;">
                                    Vozniski.si
                                </h1>
                            </td>
                        </tr>
                        <!-- Greeting & Intro -->
                        <tr>
                            <td style="padding-bottom: 30px; text-align: center;">
                                <h2 style="margin: 0 0 10px 0; font-size: 24px; font-weight: bold; color: {TEXT_WHITE};">
                                    Hitro se prijavi!
                                </h2>
                                <p style="margin: 0; font-size: 16px; line-height: 1.5; color: {TEXT_GRAY};">
                                    Našli smo <strong style="color: {TEXT_WHITE}">{n}</strong> novih terminov, ki ustrezajo vašim željam:
                                </p>
                                <p style="margin: 8px 0 0 0; font-size: 14px; font-weight: 500; color: {ACCENT}; text-transform: uppercase; letter-spacing: 0.5px;">
                                    {label_loc} &bull; {label_cat}
                                </p>
                            </td>
                        </tr>
                        
                        <!-- Slots List -->
                        {slots_html}
                        
                        <!-- Footer -->
                        <tr>
                            <td style="border-top: 1px solid {BORDER}; padding-top: 20px; text-align: center;">
                                <p style="font-size: 12px; color: {TEXT_GRAY}; margin: 0 0 10px 0;">
                                    To sporočilo ste prejeli, ker ste naročeni na obvestila na Vozniski.si.
                                </p>
                                {f'<p style="font-size: 12px; margin: 0;"><a href="{FRONTEND_UNSUB_BASE}?token={unsub_token}" style="color: {TEXT_GRAY}; text-decoration: underline;">Odjava od obvestil</a></p>' if unsub_token else ''}
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    return subject, text, html

def _fetch_active_subscriptions() -> List[Dict[str, Any]]:
    res = post_to_convex("notifications/subscriptions", {})
    if not res or not res.get("ok"):
        _log("Failed to fetch active subscriptions from Convex")
        return []
    subscriptions = list(res.get("subscriptions") or [])
    _log(f"Fetched active subscriptions count={len(subscriptions)}")
    return subscriptions

def _parse_slot_date(slot: Dict[str, Any]) -> Optional[datetime]:
    try:
        return datetime.strptime(str(slot.get("date_str") or "").strip(), "%d. %m. %Y")
    except Exception:
        return None

def _slot_within_notification_window(
    slot: Dict[str, Any],
    scrape_ts: datetime,
    max_days: int = NOTIFICATION_WINDOW_DAYS,
) -> bool:
    return slot_within_notification_window(slot, scrape_ts, max_days)

def _canonical_subscriptions(subs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return canonical_subscriptions(subs)

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
        _log("No slot changes from sync; skipping subscriber notifications")
        return 0

    subs = _canonical_subscriptions(_fetch_active_subscriptions())
    if not subs:
        _log("No active subscriptions after canonicalization; skipping subscriber notifications")
        return 0

    # Build one deduplicated slot bucket per canonical subscription (one per email).
    by_sub: dict[str, dict[str, Dict[str, Any]]] = {}
    out_of_window = 0
    match_count = 0
    for slot in changes:
        if not _slot_within_notification_window(slot, scrape_ts):
            out_of_window += 1
            continue
        for sub in subs:
            if not sub.get("active", True):
                continue
            if _match(sub, slot):
                match_count += 1
                slot_key = "|".join([
                    str(slot.get("date_str") or ""),
                    str(slot.get("time_str") or ""),
                    str(slot.get("location") or ""),
                    str(slot.get("categories") or ""),
                    str(slot.get("exam_type") or ""),
                ])
                by_sub.setdefault(str(sub["id"]), {})[slot_key] = slot

    if not by_sub:
        _log(
            f"No matching subscriptions for changes={len(changes)} "
            f"canonical_subscriptions={len(subs)} out_of_window={out_of_window}"
        )
        return 0

    sent = 0
    failed = 0
    for sub in subs:
        sid = str(sub["id"])
        items = list((by_sub.get(sid) or {}).values())
        if not items:
            continue
        subject, text, html = _render_email(sub, items)
        ok = _resend_send(sub["email"], subject, html, text)
        if ok:
            sent += 1
            # best-effort: update last_notified_at
            post_to_convex(
                "notifications/subscription/notified",
                {"id": sid, "last_notified_at": scrape_ts.isoformat()},
            )
        else:
            failed += 1

    _log(
        f"Subscriber notification result changes={len(changes)} "
        f"canonical_subscriptions={len(subs)} matched_pairs={match_count} "
        f"out_of_window={out_of_window} sent={sent} failed={failed}"
    )
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
        lines.append(" * " + _slot_text_line(it))
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
    if not RESEND_API_KEY:
        return False

    day_label, start_iso_utc, end_iso_utc = _dt_range_for_local_day(now_utc, "Europe/Ljubljana")
    marker_msg = f"daily_summary_sent {day_label}"

    marker = post_to_convex("scrape/log/marker", {"message": marker_msg})
    if marker and marker.get("exists"):
        return False

    res = post_to_convex("scrape/logs/range", {"start": start_iso_utc, "end": end_iso_utc})
    if not res or not res.get("ok"):
        return False
    rows = list(res.get("logs") or [])

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
    store_scrape_log(opened=0, updated=0, total=0, success=True, message=marker_msg)

    return True
