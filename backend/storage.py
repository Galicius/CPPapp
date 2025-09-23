from datetime import datetime
from typing import Optional
from sqlmodel import Field, SQLModel, create_engine, Session, select
from sqlalchemy import text
import os
from sqlmodel import create_engine

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///slots.db")

# Force a writable, per-instance path when using SQLite in Cloud Run
if DATABASE_URL.startswith("sqlite:///"):
    DATABASE_URL = "sqlite:////tmp/slots.db"

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30},
)

class Slot(SQLModel, table=True):
    __tablename__ = "slot"  # type: ignore[assignment]
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)

    date_str: str
    time_str: str

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
    if DATABASE_URL.startswith("sqlite:"):
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
                row = Slot(
                    date_str=it["date_str"],
                    time_str=it["time_str"],
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
                # update mutable fields
                row.exam_type = it.get("exam_type", row.exam_type)
                row.places_left = pl if pl is not None else row.places_left
                row.tolmac = bool(it.get("tolmac", row.tolmac))
                row.categories = it.get("categories", row.categories)
                row.source_page = it.get("source_page", row.source_page)
                row.location = it.get("location", row.location)
                row.available = (row.places_left or 0) > 0
                row.updated_at = now
                row.last_seen_at = scrape_ts
                updated += 1

        ses.commit()

    return opened, updated, seen_keys, scrape_ts


def finalize_scrape(scrape_ts: datetime):
    now = datetime.utcnow()
    with Session(engine) as ses:
        # Execute raw SQL for bulk update using SQLAlchemy's execute method
        ses.execute(text("""
            update slot
            set places_left = 0,
                available = false,
                updated_at = :now
            where (last_seen_at is null or last_seen_at < :ts)
              and available = true
        """), {"ts": scrape_ts, "now": now})
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