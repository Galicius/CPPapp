# api.py
import os
import time
import logging
from datetime import datetime
from fastapi import FastAPI, Query, Header, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from scraper import fetch_all_pages
from storage import fetch_slots_from_convex

DEFAULT_SLOTS_EXTRAS = "places_left,exam_type,tolmac,obmocje,town"
SCHED_SA = "scheduler-cppapp@hackaton-421720.iam.gserviceaccount.com"
SCRAPE_SECRET = os.getenv("SCRAPE_SECRET", "")

def _is_authorized(req: Request) -> bool:
    email = req.headers.get("X-Serverless-Authorization-Email") or req.headers.get("X-Goog-Authenticated-User-Email")
    if email and SCHED_SA in email:
        return True
    secret = req.headers.get("X-Secret", "")
    from os import getenv
    return bool(getenv("SCRAPE_SECRET") and secret == getenv("SCRAPE_SECRET"))

app = FastAPI(title="SlotWatch API")

# CORS: use host only, no trailing slash; env var overrides if set
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("FRONTEND_ORIGINS", "").split(",") if o.strip()] \
                  or ["https://examalert.vercel.app"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

log = logging.getLogger("uvicorn.error")

@app.post("/admin/trigger-scrape")
def trigger(
    request: Request,
    background_tasks: BackgroundTasks,
    x_secret: str | None = Header(default=None)
):
    if not _is_authorized(request):
        log.warning("DENY /admin/trigger-scrape email=%s has_secret=%s",
                    request.headers.get("X-Goog-Authenticated-User-Email"),
                    bool(x_secret))
        raise HTTPException(status_code=403, detail="forbidden")

    # ✅ schedule background job instead of blocking
    background_tasks.add_task(run_scraper_job)
    return {"status": "accepted"}

def run_scraper_job():
    start_time = time.time()
    try:
        from scraper import fetch_all_pages
        from storage import (
            store_scrape_log, sync_slots_to_convex, mark_absent_in_convex,
            revalidate_slots_cache,
        )

        slots, pages_scraped = fetch_all_pages()
        scrape_ts = datetime.utcnow()
        sync_result = sync_slots_to_convex(slots, scrape_ts)
        opened = int(sync_result.get("opened") or 0)
        updated = int(sync_result.get("updated") or 0)
        changes = list(sync_result.get("changes") or [])
        mark_absent_in_convex(scrape_ts)
        revalidate_slots_cache()

        # --- Notifications ---
        notification_stats = None
        try:
            from notifications import notify_subscribers_for_changes
            notification_stats = notify_subscribers_for_changes(changes, scrape_ts)
            log.info(f"Notifications sent: subs={notification_stats.get('sent', 0)}")
        except Exception as ne:
            log.warning(f"Notification step failed: {ne}")

        duration = time.time() - start_time
        store_scrape_log(
            opened=opened,
            updated=updated,
            total=len(slots),
            success=True,
            message="background scrape success",
            duration_seconds=duration,
            pages_scraped=pages_scraped,
            notification_stats=notification_stats,
        )

        try:
            from notifications import send_daily_summary_if_due
            daily_sent = send_daily_summary_if_due(scrape_ts)  # once/day daily rollup
            log.info(f"Daily summary attempted: sent={daily_sent}")
        except Exception as ne:
            log.warning(f"Daily summary step failed: {ne}")

        log.info(f"Scrape done: opened={opened}, updated={updated}, total={len(slots)}, pages={pages_scraped}, dur={duration:.2f}s")

    except Exception as e:
        log.exception("Background scrape failed")
        try:
            store_scrape_log(0, 0, 0, success=False, message=str(e))
        except Exception:
            pass


@app.get("/healthz")
def health():
    return {"ok": True, "database": "convex"}

@app.get("/slots_all")
def slots_all(
    cat: str | None = Query(default=None, description="Comma categories, e.g. B,B1"),
    region: str | None = Query(default=None, description="Contains 'Območje X'"),
    limit: int | None = Query(default=None, description="Optional max items"),
    include_fields: str | None = Query(default=None, description="Comma list of extra fields: obmocje,town,exam_type,places_left,tolmac,source_page,created_at,updated_at"),
):
    def _d(s: str): return datetime.strptime(s.strip(), "%d. %m. %Y").date()
    def _t(s: str | None): return datetime.strptime((s or "00:00").strip(), "%H:%M").time()

    data = fetch_slots_from_convex()
    rows = data.get("items") or []

    items = []
    for row in rows:
        try:
            d = _d(row.get("date_str") or "")
        except Exception:
            continue
        if region:
            try:
                if int(row.get("obmocje") or -1) != int(region):
                    continue
            except ValueError:
                pass
        items.append((d, _t(row.get("time_str")), row))

    if cat:
        want = {x.strip() for x in cat.split(",") if x.strip()}
        items = [t for t in items if want & set(t[2]["categories"].split(","))]
    if region:
        items = [t for t in items if t[2]["location"] and f"Območje {region}" in t[2]["location"]]

    items.sort(key=lambda x: (x[0], x[1]))
    out = [t[2] for t in items]
    if isinstance(limit, int) and limit > 0:
        out = out[:min(limit, 10000)]
    return {"last_scraped_at": data.get("last_scraped_at"), "count": len(out), "items": out}
