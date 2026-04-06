from datetime import datetime, date, time
from typing import Optional
from sqlmodel import Field, SQLModel, create_engine, Session, select
from sqlalchemy import text
import os
import sys
import logging
from storage_helpers import to_int_or_none



log_logger = logging.getLogger(__name__)

def log_stderr(msg: str):
    """Timestamped log to stderr for cloud visibility."""
    ts = datetime.utcnow().isoformat()
    print(f"[{ts}] [STORAGE] {msg}", file=sys.stderr, flush=True)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Missing required environment variable: DATABASE_URL")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

class Slot(SQLModel, table=True):
    __tablename__ = "slot"  # type: ignore[assignment]
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)

    date_str: str
    time_str: str

    # normalized for sorting/filtering
    date_iso: Optional[date] = Field(default=None, index=True)
    time_iso: Optional[time] = Field(default=None, index=True)

    # new fields
    obmocje: Optional[int] = Field(default=None, index=True)
    town: Optional[str] = Field(default=None, index=True)
    exam_type: Optional[str] = Field(default=None, index=True)     # "voznja" | "teorija"
    places_left: Optional[int] = Field(default=None)
    tolmac: bool = Field(default=False)

    categories: str = Field(index=True)                             # "B,B1" etc.
    source_page: Optional[int] = None

    # derived location for backwards compatibility / display
    location: Optional[str] = Field(default=None, index=True)

    # flags / timestamps
    available: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: Optional[datetime] = Field(default=None, index=True)

def _make_key(it: dict) -> tuple:
    return (
        it.get("date_str"),
        it.get("time_str"),
        it.get("obmocje"),
        (it.get("town") or "").strip().lower(),
        (it.get("categories") or ""),
    )

def _to_int_or_none(val) -> Optional[int]:
    """
    Safely converts 'Še 1', '1', or 1 → 1.
    Returns None if conversion fails or no digits are found.
    Keeps storage robust even if scraper changes.
    """
    return to_int_or_none(val)

def store_scrape_log(opened: int, updated: int, total: int, success: bool, message: str = "", duration_seconds: float = 0.0, pages_scraped: int = 0) -> bool:
    """
    Best-effort write of a scrape summary to Supabase.
    Reads SUPABASE_URL and SUPABASE_SERVICE_KEY from env,
    creates the client, and calls log_scrape_result(...).

    Returns True if inserted, False if skipped/failed.
    """
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log_stderr("Supabase env not set; skipping scrape log")
        return False

    try:
        from supabase import create_client
    except Exception as e:
        log_stderr(f"Supabase SDK not available; skipping scrape log: {e}")
        return False

    try:
        sb = create_client(url, key)
        log_scrape_result(sb, opened=opened, updated=updated, total=total, success=success, message=message, duration_seconds=duration_seconds, pages_scraped=pages_scraped)
        return True
    except Exception as e:
        log_stderr(f"Supabase insert failed: {e}")
        return False


def upsert_slots(items: list[dict]) -> tuple[int, int, set[tuple], datetime, list[dict]]:
    """
    Insert new or update existing slots by unique key.
    Meaningful change = slot appears or disappears (presence).
    Returns (opened, updated, seen_keys, scrape_ts).

    OPTIMIZED: Batched lookup to avoid N+1 queries.
    """
    log_stderr(f"START upsert_slots items={len(items)}")
    now = datetime.utcnow()
    scrape_ts = now
    opened = updated = 0
    seen_keys: set[tuple] = set()
    new_or_reappeared: list[dict] = []

    if not items:
         return opened, updated, seen_keys, scrape_ts, []

    # Prepare keys for batch lookup
    # We can fetch all potentially matching slots.
    # Since we lack a single unique ID, and composite keys are complex to Query via IN clause across multiple columns in standard SQLModel easily...
    # We will fetch all slots that match the *dates* present in the scrape.
    # Typically this is a small range of dates (next 45 days).
    
    # 1. Collect unique dates from items
    # Normalize dates first for querying
    unique_dates = set()
    for it in items:
        try:
             d = datetime.strptime(it["date_str"].strip(), "%d. %m. %Y").date()
             unique_dates.add(d)
        except Exception:
             pass
    
    # 2. Batch fetch existing slots
    # If no valid dates found (weird?), fail back to empty or handle gracefully
    existing_map = {}
    
    with Session(engine) as ses:
        # Fetch all slots for these dates
        # This assumes date_iso is populated correctly on existing slots.
        if unique_dates:
            chunks = list(unique_dates)
            # Fetch in chunks if too many dates (unlikely for 45 days, but safe)
            # SQLAlchemy IN clause is fine with a list
            
            # Using raw strings or simpler logic if needed, but SQLModel verify:
            q = select(Slot).where(Slot.date_iso.in_(chunks))
            existing_rows = ses.exec(q).all()
            
            # Index them by the composite key
            for row in existing_rows:
                k = (
                    row.date_str,
                    row.time_str,
                    row.obmocje,
                    (row.town or "").strip().lower() if row.town else None,
                    row.categories or "",
                )
                existing_map[k] = row

        # 3. Process items in memory
        for it in items:
            # derive location if not provided
            if "location" not in it:
                if it.get("obmocje") is not None:
                    loc = f"Območje {it['obmocje']}"
                    if it.get("town"):
                        loc += f" , {it['town']}"
                    it["location"] = loc
                else:
                    it["location"] = None

            # places_left normalization (for display only)
            pl = _to_int_or_none(it.get("places_left"))            

            # stable natural key for identity
            key = (
                it["date_str"],
                it["time_str"],
                it.get("obmocje"),
                (it.get("town") or "").strip().lower(),
                it.get("categories", ""),
            )
            seen_keys.add(key)

            row = existing_map.get(key)

            if row is None:
                # parse normalized fields (best-effort)
                try:
                    _d = datetime.strptime(it["date_str"].strip(), "%d. %m. %Y").date()
                except Exception:
                    _d = None
                try:
                    _t = datetime.strptime((it["time_str"] or "00:00").strip(), "%H:%M").time()
                except Exception:
                    _t = None

                row = Slot(
                    date_str=it["date_str"],
                    time_str=it["time_str"],
                    date_iso=_d,
                    time_iso=_t,
                    obmocje=it.get("obmocje"),
                    town=it.get("town"),
                    exam_type=it.get("exam_type"),
                    places_left=pl,
                    tolmac=bool(it.get("tolmac")),
                    categories=it.get("categories", ""),
                    source_page=it.get("source_page"),
                    location=it.get("location"),

                    # presence semantics:
                    available=True,       # present in this scrape
                    created_at=now,       # first seen
                    updated_at=now,       # appeared (nonexistent -> present)
                    last_seen_at=scrape_ts,
                )
                                # record newly opened slot
                new_or_reappeared.append({
                    "date_str": it["date_str"],
                    "time_str": it["time_str"],
                    "obmocje": it.get("obmocje"),
                    "town": it.get("town"),
                    "exam_type": it.get("exam_type"),
                    "places_left": pl,
                    "tolmac": bool(it.get("tolmac")),
                    "categories": it.get("categories", "") or "",
                    "location": it.get("location"),
                })

                ses.add(row)
                # update map so duplicates in same batch use same row object (if any?)
                existing_map[key] = row 
                opened += 1
            else:
                # heartbeat every scrape
                row.last_seen_at = scrape_ts

                # if it was previously unavailable, it reappeared -> meaningful change
                if not row.available:
                    row.available = True
                                        # record reappeared (was unavailable -> now available)
                    new_or_reappeared.append({
                        "date_str": it["date_str"],
                        "time_str": it["time_str"],
                        "obmocje": it.get("obmocje"),
                        "town": it.get("town"),
                        "exam_type": it.get("exam_type"),
                        "places_left": pl if pl is not None else row.places_left,
                        "tolmac": bool(it.get("tolmac")) if "tolmac" in it else bool(row.tolmac),
                        "categories": it.get("categories", row.categories) or "",
                        "location": it.get("location", row.location),
                    })

                    row.updated_at = now
                    updated += 1

                # keep display fields fresh WITHOUT bumping updated_at
                if pl is not None:
                    row.places_left = pl

                # (Optional) keep static/descriptive fields in sync without bumping updated_at
                if "exam_type" in it:
                    row.exam_type = it["exam_type"]
                if "tolmac" in it:
                    row.tolmac = bool(it["tolmac"])
                if "categories" in it:
                    row.categories = it["categories"]
                if "source_page" in it:
                    row.source_page = it["source_page"]
                if "location" in it:
                    row.location = it["location"]
                
                # Ensure existing objects are attached if session was fresh (it is)
                ses.add(row)

        ses.commit()
    
    log_stderr(f"END upsert_slots opened={opened}, updated={updated}")
    return opened, updated, seen_keys, scrape_ts, new_or_reappeared



from sqlalchemy import text
def finalize_scrape(scrape_ts: datetime):
    log_stderr(f"START finalize_scrape, marking absent slots < {scrape_ts}")
    now = datetime.utcnow()
    stmt = text("""
        UPDATE slot
        SET
            available = FALSE,
            places_left = 0,
            updated_at = :now
        WHERE
            available = TRUE
            AND (last_seen_at IS NULL OR last_seen_at < :scrape_ts)
    """).bindparams(now=now, scrape_ts=scrape_ts)

    with Session(engine) as ses:
        res = ses.exec(stmt)   # ← no extra dict
        ses.commit()
        log_stderr(f"END finalize_scrape, rows matched/affected={res.rowcount}") # rowcount is best effort


class ScrapeMeta(SQLModel, table=True):
    __tablename__ = "scrapemeta"  # type: ignore[assignment]
    __table_args__ = {"extend_existing": True}

    id: int = Field(default=1, primary_key=True)
    last_scraped_at: datetime = Field(default_factory=datetime.utcnow, index=True)

def log_scrape_result(client, opened: int, updated: int, total: int, success: bool, message: str = "", duration_seconds: float = 0.0, pages_scraped: int = 0):
    """
    Store scrape metadata and result details in Supabase.
    """
    try:
        now = datetime.utcnow().isoformat()
        client.table("scrape_logs").insert({
            "timestamp": now,
            "opened": opened,
            "updated": updated,
            "total": total,
            "success": success,
            "message": message,
            "duration_seconds": duration_seconds,
            "pages_scraped": pages_scraped,
        }).execute()
    except Exception as e:
        # Fallback logging if Supabase fails
        log_stderr(f"[WARN] Failed to log scrape result to Supabase: {e}")



def get_last_scraped_at() -> Optional[datetime]:
    with Session(engine) as ses:
        meta = ses.get(ScrapeMeta, 1)
        return meta.last_scraped_at if meta else None

def set_last_scraped_at(ts: datetime):
    with Session(engine) as ses:
        meta = ses.get(ScrapeMeta, 1)
        if not meta:
            meta = ScrapeMeta(id=1, last_scraped_at=ts)
            ses.add(meta)
        else:
            meta.last_scraped_at = ts
        ses.commit()


# --- Supabase ---

def _get_supabase_client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception as e:
        log_stderr(f"Supabase SDK not available: {e}")
        return None


def _normalize_dt_fields(it: dict):
    # Mirror your local normalization for date_iso/time_iso without mutating originals
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


def sync_slots_to_supabase(items: list[dict], scrape_ts: datetime) -> bool:
    """
    Upsert latest view into public.slots_current and append snapshot into public.slots_history.
    Best-effort; logs warning on failure and returns False.
    """
    log_stderr(f"START sync_slots_to_supabase items={len(items)}")
    sb = _get_supabase_client()
    if not sb:
        log_stderr("Supabase env not set or client missing; skipping slot sync")
        return False

    if not items:
        return True

    # Prepare batches (normalize date_iso/time_iso like local storage does)
    rows_current = []
    rows_history = []
    now = datetime.utcnow().isoformat()

    for it in items:
        rec = _normalize_dt_fields(it)
        base = {
            "date_str": rec["date_str"],
            "time_str": rec["time_str"],
            "date_iso": rec["date_iso"],
            "time_iso": rec["time_iso"],
            "obmocje": rec.get("obmocje"),
            "town": rec.get("town"),
            "exam_type": rec.get("exam_type"),
            "places_left": _to_int_or_none(rec.get("places_left")),
            "tolmac": bool(rec.get("tolmac")),
            "categories": rec.get("categories", "") or "",
            "source_page": rec.get("source_page"),
            "location": rec.get("location"),
            "available": True,
            "last_seen_at": scrape_ts.isoformat(),
            "updated_at": now,
        }
        rows_current.append({**base, "created_at": now})
        rows_history.append({**base, "scrape_ts": scrape_ts.isoformat()})

    try:
        # Upsert current with natural key
        # NOTE: on_conflict columns must match unique constraint defined in SQL.
        sb.table("slots_current") \
          .upsert(rows_current, on_conflict="date_str,time_str,obmocje,town,categories") \
          .execute()

        # Append history
        # Insert in chunks to avoid payload limits
        CHUNK = 1000
        for i in range(0, len(rows_history), CHUNK):
            sb.table("slots_history").insert(rows_history[i:i+CHUNK]).execute()
        
        log_stderr("END sync_slots_to_supabase success")
        return True
    except Exception as e:
        log_stderr(f"Supabase slot sync failed: {e}")
        return False


def mark_absent_in_supabase(scrape_ts: datetime) -> bool:
    """
    Mirror finalize_scrape semantics into slots_current:
    any row not touched in this scrape becomes unavailable.
    Requires last_seen_at to be set to this scrape's ts for present rows.
    """
    log_stderr("START mark_absent_in_supabase")
    sb = _get_supabase_client()
    if not sb:
        return False
    try:
        # Use RPC or direct update via filter
        # Supabase python client supports .update().neq()/lt() filters
        # Mark rows with last_seen_at < scrape_ts as unavailable and places_left=0.
        sb.table("slots_current") \
          .update({"available": False, "places_left": 0, "updated_at": datetime.utcnow().isoformat()}) \
          .lt("last_seen_at", scrape_ts.isoformat()) \
          .execute()
        
        log_stderr("END mark_absent_in_supabase success")
        return True
    except Exception as e:
        log_stderr(f"Supabase finalize mirror failed: {e}")
        return False

# --- Convex Sync ---

import requests

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
        "Content-Type": "application/json"
    }

def sync_slots_to_convex(items: list[dict], scrape_ts: datetime) -> bool:
    """
    Pushes the newly scraped slots to Convex via HTTP Action.
    """
    log_stderr(f"START sync_slots_to_convex items={len(items)}")
    url = _get_convex_url("syncSlots")
    headers = _get_convex_headers()
    
    if not url or not headers.get("Authorization"):
        log_stderr("Convex env not set; skipping slot sync")
        return False

    if not items:
        return True

    rows_current = []
    
    # Format dates to string for JSON serialization
    for it in items:
        rec = _normalize_dt_fields(it)
        rows_current.append({
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
            "location": rec.get("location")
        })

    try:
        response = requests.post(
            url, 
            json={"items": rows_current, "scrape_ts": scrape_ts.isoformat()},
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        log_stderr("END sync_slots_to_convex success")
        return True
    except Exception as e:
        log_stderr(f"Convex slot sync failed: {e}")
        return False

def mark_absent_in_convex(scrape_ts: datetime) -> bool:
    """
    Triggers Convex to mark un-seen slots as unavailable.
    """
    log_stderr("START mark_absent_in_convex")
    url = _get_convex_url("markAbsent")
    headers = _get_convex_headers()
    
    if not url or not headers.get("Authorization"):
        return False
        
    try:
        response = requests.post(
            url, 
            json={"scrape_ts": scrape_ts.isoformat()},
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        log_stderr("END mark_absent_in_convex success")
        return True
    except Exception as e:
        log_stderr(f"Convex finalize mirror failed: {e}")
        return False

