# api.py
import os
from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select
from storage import init_db, engine, Slot, upsert_slots
from scraper import fetch_all_pages


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
    limit: int = 10,
):
    with Session(engine) as ses:
        q = select(Slot).where(Slot.available == True).order_by(Slot.date_str, Slot.time_str)
        rows = ses.exec(q).all()
        items = [
            {"date_str": s.date_str, "time_str": s.time_str, "location": s.location, "categories": s.categories}
            for s in rows
        ]
    # simple in-API filtering to mirror frontend
    if cat:
        selected = set(x.strip() for x in cat.split(",") if x.strip())
        items = [it for it in items if selected & set(it["categories"].split(","))]
    if region:
        items = [it for it in items if f"Območje {region}" in it["location"]]
    return items[: max(1, min(limit, 50))]

