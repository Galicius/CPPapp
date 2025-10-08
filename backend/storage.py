from datetime import datetime, date, time
from typing import Optional
from sqlmodel import Field, SQLModel, create_engine, Session, select
from sqlalchemy import text
import os
from sqlmodel import create_engine

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///slots.db")

# Put SQLite db under /tmp when running in containers
if DATABASE_URL.startswith("sqlite:///") and not DATABASE_URL.startswith("sqlite:////"):
    DATABASE_URL = "sqlite:////tmp/slots.db"

IS_SQLITE = DATABASE_URL.startswith("sqlite:")

if IS_SQLITE:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
else:
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

def init_db():
    # SQLite pragmas for concurrency
    if IS_SQLITE:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            conn.exec_driver_sql("PRAGMA busy_timeout=30000;")
    SQLModel.metadata.create_all(engine)
    # ensure a singleton row exists for scrape meta
    with Session(engine) as ses:
        meta = ses.get(ScrapeMeta, 1)
        if not meta:
            ses.add(ScrapeMeta(id=1, last_scraped_at=datetime.utcnow()))
            ses.commit()

def _make_key(it: dict) -> tuple:
    return (
        it.get("date_str"),
        it.get("time_str"),
        it.get("obmocje"),
        (it.get("town") or "").strip().lower(),
        (it.get("categories") or ""),
    )


def upsert_slots(items: list[dict]) -> tuple[int, int, set[tuple], datetime]:
    """
    Insert new or update existing slots by unique key.
    Meaningful change = slot appears or disappears (presence).
    Returns (opened, updated, seen_keys, scrape_ts).

    - created_at: first time we saw the slot
    - updated_at: only when availability flips (disappears or reappears)
    - last_seen_at: set on every scrape when slot is present
    - available: True iff present in the *latest* scrape
    """
    now = datetime.utcnow()
    scrape_ts = now
    opened = updated = 0
    seen_keys: set[tuple] = set()

    with Session(engine) as ses:
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
            pl = it.get("places_left")
            try:
                pl = int(pl) if pl is not None else None
                    # keep None when not parseable
            except Exception:
                pl = None

            # stable natural key for identity
            key = (
                it["date_str"],
                it["time_str"],
                it.get("obmocje"),
                (it.get("town") or None),
                it.get("categories", ""),
            )
            seen_keys.add(key)

            # find existing
            q = select(Slot).where(
                Slot.date_str == it["date_str"],
                Slot.time_str == it["time_str"],
                Slot.obmocje == it.get("obmocje"),
                Slot.town == (it.get("town") or None),
                Slot.categories == it.get("categories", ""),
            )
            row = ses.exec(q).first()

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
                ses.add(row)
                opened += 1
            else:
                # heartbeat every scrape
                row.last_seen_at = scrape_ts

                # if it was previously unavailable, it reappeared -> meaningful change
                if not row.available:
                    row.available = True
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

                # (Optional) re-derive normalized date/time without bumping updated_at
                try:
                    _d = datetime.strptime(it["date_str"].strip(), "%d. %m. %Y").date()
                    row.date_iso = _d
                except Exception:
                    pass
                try:
                    _t = datetime.strptime((it["time_str"] or "00:00").strip(), "%H:%M").time()
                    row.time_iso = _t
                except Exception:
                    pass

        ses.commit()

    return opened, updated, seen_keys, scrape_ts



from sqlalchemy import text
def finalize_scrape(scrape_ts: datetime):
    """
    After each scrape finishes, mark any slots that were not seen
    in this scrape as unavailable (they disappeared).

    - A slot is considered disappeared if it was previously available
      but its last_seen_at is older than the current scrape timestamp.
    - When a slot disappears, we set:
        available = False
        places_left = 0
        updated_at = now  (since it's a meaningful change)
    """
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
    """)

    with Session(engine) as ses:
        ses.exec(stmt, {"now": now, "scrape_ts": scrape_ts})
        ses.commit()



class ScrapeMeta(SQLModel, table=True):
    __tablename__ = "scrapemeta"  # type: ignore[assignment]
    __table_args__ = {"extend_existing": True}

    id: int = Field(default=1, primary_key=True)
    last_scraped_at: datetime = Field(default_factory=datetime.utcnow, index=True)

def log_scrape_result(client, opened: int, updated: int, total: int, success: bool, message: str = ""):
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
        }).execute()
    except Exception as e:
        # Fallback logging if Supabase fails
        print(f"[WARN] Failed to log scrape result to Supabase: {e}")



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