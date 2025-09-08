# scheduler_run.py
from apscheduler.schedulers.blocking import BlockingScheduler
from storage import init_db, upsert_slots
from scraper import fetch_all_pages
from datetime import datetime
from zoneinfo import ZoneInfo  # Python 3.9+

# Quiet hours window
QUIET_START = 22  # 23:00 inclusive
QUIET_END = 6     # 07:00 exclusive
TZ = ZoneInfo("Europe/Ljubljana")


def within_quiet_hours(now: datetime) -> bool:
    """Return True if current time is within quiet hours."""
    h = now.hour
    # Wraps past midnight (23..23:59 or 00..06:59)
    return (h >= QUIET_START) or (h < QUIET_END)


def job():
    now = datetime.now(TZ)
    if within_quiet_hours(now):
        print(f"[skip] quiet hours: {now.isoformat(timespec='minutes')}")
        return

    slots = fetch_all_pages()
    opened, _ = upsert_slots(slots)
    print({"opened": opened, "total": len(slots)})


if __name__ == "__main__":
    init_db()
    sched = BlockingScheduler(timezone=TZ)
    # Run every 30 minutes during the whole day,
    # but `job()` will self-skip between 23:00–07:00.
    sched.add_job(job, "cron", minute="0,30")
    print("Running every 30 minutes (skips 23:00–07:00, Europe/Ljubljana)…")
    sched.start()
