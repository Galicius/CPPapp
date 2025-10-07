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
    Returns (opened, updated, seen_keys, scrape_ts).
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

            # places_left normalization
            pl = it.get("places_left")
            try:
                pl = int(pl) if pl is not None else None
            except Exception:
                pl = None
            available = (pl or 0) > 0

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
                # parse normalized fields
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
                    available=available,
                    created_at=now,
                    updated_at=now,
                    last_seen_at=scrape_ts,
                )
                ses.add(row)
                opened += 1
        else:
            # update mutable fields - only bump updated_at if something changed
            did_change = False

            # exam_type
            new_exam_type = it.get("exam_type", row.exam_type)
            if new_exam_type != row.exam_type:
                row.exam_type = new_exam_type
                did_change = True

            # places_left (None means "no new data" → keep old)
            if pl is not None and pl != row.places_left:
                row.places_left = pl
                did_change = True

            # tolmac
            new_tolmac = bool(it.get("tolmac", row.tolmac))
            if new_tolmac != row.tolmac:
                row.tolmac = new_tolmac
                did_change = True

            # categories
            new_categories = it.get("categories", row.categories)
            if new_categories != row.categories:
                row.categories = new_categories
                did_change = True

            # source_page
            new_source_page = it.get("source_page", row.source_page)
            if new_source_page != row.source_page:
                row.source_page = new_source_page
                did_change = True

            # location (derived/back-compat)
            new_location = it.get("location", row.location)
            if new_location != row.location:
                row.location = new_location
                did_change = True

            # available depends on places_left
            new_available = (row.places_left or 0) > 0
            if new_available != row.available:
                row.available = new_available
                did_change = True

            # keep normalized fields in sync (no change flag; these are derived)
            try:
                parsed_d = datetime.strptime(it["date_str"].strip(), "%d. %m. %Y").date()
                if parsed_d != row.date_iso:
                    row.date_iso = parsed_d
            except Exception:
                pass
            try:
                parsed_t = datetime.strptime((it["time_str"] or "00:00").strip(), "%H:%M").time()
                if parsed_t != row.time_iso:
                    row.time_iso = parsed_t
            except Exception:
                pass

            # timestamps
            if did_change:
                row.updated_at = now     # bump only when data actually changed
                updated += 1             # count *changed* rows
            row.last_seen_at = scrape_ts # always mark the row as seen this scrape

        ses.commit()

    return opened, updated, seen_keys, scrape_ts


from sqlalchemy import text
def finalize_scrape(scrape_ts):
    now = datetime.utcnow()
    stmt = text("""
      update slot
      set places_left = 0, available = false, updated_at = :now
      where (last_seen_at is null or last_seen_at < :ts) and available = true
    """).bindparams(ts=scrape_ts, now=now)
    with Session(engine) as ses:
        ses.exec(stmt)
        ses.commit()



class ScrapeMeta(SQLModel, table=True):
    __tablename__ = "scrapemeta"  # type: ignore[assignment]
    __table_args__ = {"extend_existing": True}

    id: int = Field(default=1, primary_key=True)
    last_scraped_at: datetime = Field(default_factory=datetime.utcnow, index=True)

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