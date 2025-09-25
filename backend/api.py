# api.py
import os
import logging
from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select
from sqlalchemy import text
from storage import init_db, engine, Slot, upsert_slots, finalize_scrape, IS_SQLITE
from scraper import fetch_all_pages
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlmodel import Session, select
from storage import init_db, engine, Slot, upsert_slots, get_last_scraped_at, finalize_scrape, set_last_scraped_at

DEFAULT_SLOTS_EXTRAS = "places_left,exam_type,tolmac,obmocje,town"


app = FastAPI(title="SlotWatch API")

FRONTEND_ORIGINS="https://najditermin.vercel.app/"
_frontend_origins = os.getenv("FRONTEND_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _frontend_origins.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    if "created_at" in extra:  it["created_at"] = s.created_at.isoformat(timespec="seconds") + "Z"
    if "updated_at" in extra:  it["updated_at"] = s.updated_at.isoformat(timespec="seconds") + "Z"
    return it

SCRAPE_SECRET = os.getenv("SCRAPE_SECRET")

log = logging.getLogger(name="uvicorn.error")

@app.post("/admin/trigger-scrape")
def trigger(x_secret: str | None = Header(default=None)):
    if SCRAPE_SECRET and x_secret != SCRAPE_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        slots = fetch_all_pages()
        opened, updated, seen_keys, scrape_ts = upsert_slots(slots)
        finalize_scrape(scrape_ts)
        set_last_scraped_at(scrape_ts)
        return {"opened": opened, "updated": updated, "total": len(slots)}
    except Exception:
        log.exception("trigger-scrape failed")
        raise

@app.on_event("startup")
def _start():
    init_db()

@app.get("/healthz")
def health():
    return {"ok": True}

@app.get("/slots")
def slots(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_fields: str = Query(DEFAULT_SLOTS_EXTRAS),
):
    """
    Current (available) slots only. Supports extra fields like /slots_all via include_fields.
    """
    extra = {p.strip() for p in include_fields.split(",") if p.strip()}

    with Session(engine) as ses:
        order_cols = []
        if hasattr(Slot, "date_iso") and hasattr(Slot, "time_iso"):
            order_cols = [Slot.date_iso, Slot.time_iso, Slot.id]
        elif IS_SQLITE:
            order_cols = [Slot.date_str, Slot.time_str, Slot.id]
        else:
            order_cols = [text("to_date(date_str, 'DD. MM. YYYY')"),
                          text("time_str::time"), Slot.id]
        q = select(Slot).where(Slot.available == True).order_by(*order_cols).offset(offset).limit(limit)
        rows = ses.exec(q).all()

    items = [_serialize_slot(s, extra) for s in rows]
    last = get_last_scraped_at()
    last_iso = None
    if last:
        # last is stored as naive UTC; present in local tz
        last_iso = last.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Ljubljana")).isoformat(timespec="seconds")

    return {
        "last_scraped_at": last_iso,
        "count": len(items),
        "items": items,
    }

@app.get("/slots_all")
def slots_all(
    cat: str | None = Query(default=None, description="Comma categories, e.g. B,B1"),
    region: str | None = Query(default=None, description="Contains 'Območje X'"),
    limit: int | None = Query(default=None, description="Optional max items"),
    include_fields: str | None = Query(default=None, description="Comma list of extra fields: obmocje,town,exam_type,places_left,tolmac,source_page,created_at,updated_at"),
):
    """
    Return ALL stored slots (past + future), plus last scrape timestamp for dashboard.
    """
    tz = ZoneInfo("Europe/Ljubljana")

    # read everything (do NOT filter by available)
    with Session(engine) as ses:
        rows = ses.exec(select(Slot)).all()

    # helpers to sort by real date+time
    def _d(s: str):
        # '10. 9. 2025' -> date
        return datetime.strptime(s.strip(), "%d. %m. %Y").date()
    def _t(s: str | None):
        s = (s or "00:00").strip()
        return datetime.strptime(s, "%H:%M").time()

    # shape base fields like your /slots response for compatibility
    items = []
    for s in rows:
        try:
            d = _d(s.date_str)
        except Exception:
            continue
        it = {
            "date_str": s.date_str,
            "time_str": s.time_str,
            "location": s.location,
            "categories": s.categories,
        }

        # optionally enrich
        if include_fields:
            extra = {f.strip() for f in include_fields.split(",") if f.strip()}
            if "obmocje" in extra:     it["obmocje"] = s.obmocje
            if "town" in extra:        it["town"] = s.town
            if "exam_type" in extra:   it["exam_type"] = s.exam_type
            if "places_left" in extra: it["places_left"] = s.places_left
            if "tolmac" in extra:      it["tolmac"] = s.tolmac
            if "source_page" in extra: it["source_page"] = s.source_page
            if "created_at" in extra:  it["created_at"] = s.created_at.isoformat(timespec="seconds") + "Z"
            if "updated_at" in extra:  it["updated_at"] = s.updated_at.isoformat(timespec="seconds") + "Z"

        items.append((d, _t(s.time_str), it))

    # filters
    if cat:
        want = {x.strip() for x in cat.split(",") if x.strip()}
        items = [t for t in items if want & set(t[2]["categories"].split(","))]
    if region:
        items = [t for t in items if t[2]["location"] and f"Območje {region}" in t[2]["location"]]

    # sort by date then time
    items.sort(key=lambda x: (x[0], x[1]))

    out = [t[2] for t in items]
    if isinstance(limit, int) and limit > 0:
        out = out[: min(limit, 10000)]  # soft safety cap

    # last scrape timestamp (local time for readability)
    last = get_last_scraped_at()
    last_local = None
    if last:
        # last is stored in UTC (naive); present in Europe/Ljubljana for users
        last_local = last.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).isoformat(timespec="seconds")

    return {
        "last_scraped_at": last_local,  # e.g. "2025-09-09T00:44:13+02:00"
        "count": len(out),
        "items": out,
    }


