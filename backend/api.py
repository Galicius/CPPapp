# api.py
import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Query, Header, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select
from sqlalchemy import text
from scraper import fetch_all_pages
from storage import (
    engine, Slot, upsert_slots, set_last_scraped_at, store_scrape_log,
    get_last_scraped_at, finalize_scrape,
)

DEFAULT_SLOTS_EXTRAS = "places_left,exam_type,tolmac,obmocje,town"
SCHED_SA = "scheduler-cppapp@hackaton-421720.iam.gserviceaccount.com"
SCRAPE_SECRET = os.getenv("SCRAPE_SECRET", "")

def _is_authorized(req: Request) -> bool:
    email = req.headers.get("X-Serverless-Authorization-Email") or req.headers.get("X-Goog-Authenticated-User-Email")
    if email and "scheduler-cppapp@hackaton-421720.iam.gserviceaccount.com" in email:
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

def _serialize_slot(s: Slot, extra: set[str]):
    it = {
        "date_str": s.date_str,
        "time_str": s.time_str,
        "location": s.location,
        "categories": s.categories,
    }
    if "obmocje" in extra:     it["obmocje"] = s.obmocje
    if "town" in extra:        it["town"] = s.town
    if "exam_type" in extra:   it["exam_type"] = s.exam_type
    if "places_left" in extra: it["places_left"] = s.places_left or 0
    if "tolmac" in extra:      it["tolmac"] = bool(s.tolmac)
    if "created_at" in extra and s.created_at:
        it["created_at"] = s.created_at.isoformat(timespec="seconds")
    if "updated_at" in extra and s.updated_at:
        it["updated_at"] = s.updated_at.isoformat(timespec="seconds")
    return it

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

def run_scraper_job():
    try:
        from scraper import fetch_all_pages
        from storage import (
            upsert_slots, finalize_scrape, set_last_scraped_at, store_scrape_log,
            sync_slots_to_supabase, mark_absent_in_supabase,   # <-- add
        )

        slots = fetch_all_pages()
        opened, updated, seen_keys, scrape_ts = upsert_slots(slots)

        # Mirror to Supabase BEFORE local finalize so last_seen_at is consistent
        sync_slots_to_supabase(slots, scrape_ts)               # <-- add

        finalize_scrape(scrape_ts)
        # Mirror 'finalize' semantics to Supabase too
        mark_absent_in_supabase(scrape_ts)                     # <-- add

        set_last_scraped_at(scrape_ts)

        store_scrape_log(
            opened=opened,
            updated=updated,
            total=len(slots),
            success=True,
            message="background scrape success",
        )

        log.info(f"Scrape done: opened={opened}, updated={updated}, total={len(slots)}")

    except Exception as e:
        log.exception("Background scrape failed")
        try:
            store_scrape_log(0, 0, 0, success=False, message=str(e))
        except Exception:
            pass


@app.get("/healthz")
def health():
    return {"ok": True}

@app.get("/slots_all")
def slots_all(
    cat: str | None = Query(default=None, description="Comma categories, e.g. B,B1"),
    region: str | None = Query(default=None, description="Contains 'Območje X'"),
    limit: int | None = Query(default=None, description="Optional max items"),
    include_fields: str | None = Query(default=None, description="Comma list of extra fields: obmocje,town,exam_type,places_left,tolmac,source_page,created_at,updated_at"),
):
    tz = ZoneInfo("Europe/Ljubljana")
    with Session(engine) as ses:
        rows = ses.exec(select(Slot)).all()

    def _d(s: str): return datetime.strptime(s.strip(), "%d. %m. %Y").date()
    def _t(s: str | None): return datetime.strptime((s or "00:00").strip(), "%H:%M").time()

    items = []
    for s in rows:
        try:
            d = _d(s.date_str)
        except Exception:
            continue
        it = {"date_str": s.date_str, "time_str": s.time_str, "location": s.location, "categories": s.categories}
        if include_fields:
            extra = {f.strip() for f in include_fields.split(",") if f.strip()}
            if "obmocje" in extra:     it["obmocje"] = s.obmocje
            if "town" in extra:        it["town"] = s.town
            if "exam_type" in extra:   it["exam_type"] = s.exam_type
            if "places_left" in extra: it["places_left"] = s.places_left
            if "tolmac" in extra:      it["tolmac"] = s.tolmac
            if "source_page" in extra: it["source_page"] = s.source_page
            if "created_at" in extra and s.created_at: it["created_at"] = s.created_at.isoformat(timespec="seconds")
            if "updated_at" in extra and s.updated_at: it["updated_at"] = s.updated_at.isoformat(timespec="seconds")
        items.append((d, _t(s.time_str), it))

    if cat:
        want = {x.strip() for x in cat.split(",") if x.strip()}
        items = [t for t in items if want & set(t[2]["categories"].split(","))]
    if region:
        items = [t for t in items if t[2]["location"] and f"Območje {region}" in t[2]["location"]]

    items.sort(key=lambda x: (x[0], x[1]))
    out = [t[2] for t in items]
    if isinstance(limit, int) and limit > 0: out = out[: min(limit, 10000)]

    last = get_last_scraped_at()
    last_local = last.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).isoformat(timespec="seconds") if last else None
    return {"last_scraped_at": last_local, "count": len(out), "items": out}
