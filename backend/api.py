# api.py
import os
from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select
from storage import init_db, engine, Slot, upsert_slots
from scraper import fetch_all_pages
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo



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
        items.append((d, _parse_time(s.time_str), it))

    # Filters
    if cat:
        selected = {x.strip() for x in cat.split(",") if x.strip()}
        items = [t for t in items if selected & set(t[2]["categories"].split(","))]
    if region:
        items = [t for t in items if t[2]["location"] and f"Območje {region}" in t[2]["location"]]

    # Sort by real date then time
    items.sort(key=lambda x: (x[0], x[1]))

    # Finalize + optional limit (no hard 50 cap anymore)
    out = [t[2] for t in items]
    if isinstance(limit, int) and limit > 0:
        out = out[: min(limit, 1000)]  # safety cap if you want one
    return out

