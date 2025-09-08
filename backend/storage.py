# storage.py
from datetime import datetime
from typing import Optional
from sqlmodel import Field, SQLModel, create_engine, Session, select
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///slots.db")
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

class Slot(SQLModel, table=True):
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

    # keep a derived location for backwards compatibility / display
    location: Optional[str] = Field(default=None, index=True)

    # flags / timestamps
    available: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

def init_db():
    SQLModel.metadata.create_all(engine)

def _make_key(it: dict) -> tuple:
    """
    Unique-ish identity for a slot.
    We use date+time+obmocje+town+categories.
    """
    return (
        it.get("date_str"),
        it.get("time_str"),
        it.get("obmocje"),
        (it.get("town") or "").strip().lower(),
        (it.get("categories") or ""),
    )

def upsert_slots(items: list[dict]) -> tuple[int, int]:
    """
    Insert new or update existing slots by unique key.
    Returns (opened, updated).
    """
    now = datetime.utcnow()
    opened = updated = 0

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

            key = _make_key(it)

            # try to find existing record
            q = select(Slot).where(
                Slot.date_str == it["date_str"],
                Slot.time_str == it["time_str"],
                Slot.obmocje == it.get("obmocje"),
                (Slot.town == (it.get("town") or None)),
                Slot.categories == it.get("categories"),
            )
            row = ses.exec(q).first()

            if row is None:
                row = Slot(
                    date_str=it["date_str"],
                    time_str=it["time_str"],
                    obmocje=it.get("obmocje"),
                    town=it.get("town"),
                    exam_type=it.get("exam_type"),
                    places_left=it.get("places_left"),
                    tolmac=bool(it.get("tolmac")),
                    categories=it.get("categories", ""),
                    source_page=it.get("source_page"),
                    location=it.get("location"),
                    available=True,
                    created_at=now,
                    updated_at=now,
                )
                ses.add(row)
                opened += 1
            else:
                # update fields that may change
                row.exam_type = it.get("exam_type")
                row.places_left = it.get("places_left")
                row.tolmac = bool(it.get("tolmac"))
                row.categories = it.get("categories", row.categories)
                row.source_page = it.get("source_page", row.source_page)
                row.location = it.get("location", row.location)
                row.available = True
                row.updated_at = now
                updated += 1

        ses.commit()

    return opened, updated
