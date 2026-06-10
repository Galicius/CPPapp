# api.py
import os
from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select
from storage import init_db, engine, Slot, upsert_slots
from scraper import fetch_all_pages
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlmodel import Session, select
from storage import init_db, engine, Slot, upsert_slots, get_last_scraped_at



app = FastAPI(title="SlotWatch API")

FRONTEND_ORIGINS="https://vozniski.si/"
_frontend_origins = os.getenv("FRONTEND_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _frontend_origins.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


SCRAPE_SECRET = os.getenv("SCRAPE_SECRET")

@app.post("/admin/trigger-scrape")
def trigger(x_secret: str | None = Header(default=None)):
    if SCRAPE_SECRET and x_secret != SCRAPE_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    slots = fetch_all_pages()
    opened, updated = upsert_slots(slots)
    return {"opened": opened, "total": len(slots)}

@app.on_event("startup")
def _start():
    init_db()

@app.get("/healthz")
def health():
    return {"ok": True}

@app.get("/slots")
def list_slots(
    cat: str | None = Query(default=None, description="Comma categories, e.g. B,B1"),
    region: str | None = Query(default=None, description="Contains 'Območje X'"),
    days: int = Query(default=30, ge=1, le=90, description="How many days ahead to include"),
    limit: int | None = Query(default=None, description="Optional max items (no cap if omitted)"),
):
    tz = ZoneInfo("Europe/Ljubljana")
    today = datetime.now(tz).date()
    end = today + timedelta(days=days)

    with Session(engine) as ses:
        q = select(Slot).where(Slot.available == True)
        rows = ses.exec(q).all()

    def _parse_date(s: str):
        return datetime.strptime(s.strip(), "%d. %m. %Y").date()

    def _parse_time(s: str | None):
        s = (s or "00:00").strip()
        return datetime.strptime(s, "%H:%M").time()

    items = []
    for s in rows:
        try:
            d = _parse_date(s.date_str)
        except Exception:
            continue
        if not (today <= d <= end):
            continue

        it = {
            "date_str": s.date_str,
            "time_str": s.time_str,
            "location": s.location,
            "categories": s.categories,
        }
        items.append((d, _parse_time(s.time_str), it, s.obmocje))

    # Filters
    if cat:
        selected = {x.strip() for x in cat.split(",") if x.strip()}
        items = [t for t in items if selected & set(t[2]["categories"].split(","))]
    if region:
        items = [t for t in items if str(t[3]) == str(region)]

    # Sort by real date then time
    items.sort(key=lambda x: (x[0], x[1]))

    # Finalize + optional limit (no hard 50 cap anymore)
    out = [t[2] for t in items]
    if isinstance(limit, int) and limit > 0:
        out = out[: min(limit, 1000)]  # safety cap if you want one
    return out

@app.get("/slots_all")
def slots_all(
    cat: str | None = Query(default=None, description="Comma categories, e.g. B,B1"),
    region: str | None = Query(default=None, description="Contains 'Območje X'"),
    limit: int | None = Query(default=None, description="Optional max items"),
    include_fields: str | None = Query(default=None, description="Comma list of extra fields: obmocje,town,exam_type,places_left,tolmac,source_page,details_location,created_at,updated_at"),
):
    """
    Return ALL stored slots (past + future), plus last scrape timestamp.
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
            if "details_location" in extra: it["details_location"] = s.details_location
            if "created_at" in extra:  it["created_at"] = s.created_at.isoformat(timespec="seconds") + "Z"
            if "updated_at" in extra:  it["updated_at"] = s.updated_at.isoformat(timespec="seconds") + "Z"

        items.append((d, _t(s.time_str), it, s.obmocje))

    # filters
    if cat:
        want = {x.strip() for x in cat.split(",") if x.strip()}
        items = [t for t in items if want & set(t[2]["categories"].split(","))]
    if region:
        items = [t for t in items if str(t[3]) == str(region)]

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

