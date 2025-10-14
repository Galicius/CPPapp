# notifications.py
from __future__ import annotations

import os
import httpx
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from storage import _get_supabase_client

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_API_URL = "https://api.resend.com/emails"
MAIL_FROM = os.getenv("MAIL_FROM", "ExamAlert <onboarding@resend.dev>")
FRONTEND_UNSUB_BASE = os.getenv("FRONTEND_UNSUB_BASE", "https://examalert.vercel.app/unsubscribe")

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
        parts.append(f"tip: {it['exam_type']}")
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
    subject = f"Novi termini ({n}) — {label_loc}, {label_cat}, {label_tip}"

    # plain text
    lines = []
    lines.append("Pozdrav,")
    lines.append("")
    lines.append("Našli smo nove termine, ki ustrezajo vašim nastavitvam:")
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
    html_lines.append("<p>Pozdrav,</p>")
    html_lines.append("<p>Našli smo nove termine, ki ustrezajo vašim nastavitvam:</p>")
    if crit:
        html_lines.append("<p>" + " • ".join(crit) + "</p>")
    html_lines.append("<ul>")
    for it in items:
        html_lines.append(f"<li>{_slot_line(it)}</li>")
    html_lines.append("</ul>")
    if unsub:
        html_lines.append(f'<p>Odjava: <a href="{FRONTEND_UNSUB_BASE}?token={unsub}">{FRONTEND_UNSUB_BASE}?token={unsub}</a></p>')
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
    to = "gal.gustin@gmail.com"
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
