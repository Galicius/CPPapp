from __future__ import annotations

import os
import re
import time
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlencode
from tenacity import retry, stop_after_attempt, wait_exponential

from storage import upsert_slots

LOCAL_TZ = ZoneInfo("Europe/Ljubljana")


# -------------------- Config --------------------

BASE = "https://e-uprava.gov.si"
# new landing that sets cookies, referer, etc.
MAIN = f"{BASE}/si/javne-evidence/prosti-termini-zemljevid.html?lang=si"
# new AJAX singleton (table layout)
AJAX = f"{BASE}/si/javne-evidence/prosti-termini-zemljevid/content/singleton.html"

MAX_PAGES = 300               # hard safety cap
MAX_DAYS_AHEAD = 30           # stop when a slot's date is beyond this many days
REQUEST_PAUSE = (0.6, 1.1)    # random sleep range between pages (seconds)
DEBUG = os.getenv("DEBUG", "0") == "1"

OUTDIR = os.getenv("OUTDIR", "/tmp/debug_pages")
if DEBUG:
    os.makedirs(OUTDIR, exist_ok=True)


# -------------------- Utils --------------------

def _norm_space(s: str) -> str:
    return " ".join(s.replace("\xa0", " ").split())


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""

# Inherit the date from the nearest previous row that has the calendar cell.
def _row_date_str(tr) -> Optional[str]:
    def _from_calendar(t) -> Optional[str]:
        cal = t.select_one(".calendarBox")
        if not cal:
            return None
        s = cal.get("aria-label") or _text(cal.select_one(".sr-only")) or ""
        s = s.strip()
        if not s:
            return None
        # Be robust to aria-label like "petek, 24. 10. 2025" — extract the date part.
        m = re.search(r"\b(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\b", s)
        return m.group(1) if m else s

    # Current row
    s = _from_calendar(tr)
    if s:
        return s

    # Walk backwards until a calendar cell is found
    p = tr.find_previous("tr")
    while p:
        s = _from_calendar(p)
        if s:
            return s
        p = p.find_previous("tr")
    return None

def _compose_location(obmocje: Optional[int], town: Optional[str]) -> Optional[str]:
    if obmocje is None and not town:
        return None
    if obmocje is not None and town:
        return f"Območje {obmocje} , {town}"
    if obmocje is not None:
        return f"Območje {obmocje}"
    return town


def _parse_iso(date_str: str, time_str: str) -> Tuple[Optional[str], Optional[str]]:
    # input like "14. 10. 2025" and "8:30"
    try:
        d = datetime.strptime(date_str.strip(), "%d. %m. %Y").date()
        t = datetime.strptime(time_str.strip(), "%H:%M").time()
        return (d.isoformat(), t.strftime("%H:%M:%S"))
    except Exception:
        return (None, None)


# -------------------- Town extraction --------------------

def _clean_town(raw: str) -> Optional[str]:
    if not raw:
        return None

    obmocje_map = {
        1: ["Ajdovščina", "Idrija", "Ilirska Bistrica", "Koper", "Nova Gorica",
            "Postojna", "Sežana", "Tolmin"],
        2: ["Domžale", "Ig", "Jesenice", "Kranj", "Ljubljana", "Vrhnika"],
        3: ["Celje", "Laško", "Ločica ob Savinji", "Ravne na Koroškem",
            "Slovenske Konjice", "Slovenj Gradec", "Šentjur",
            "Šmarje pri Jelšah", "Trbovlje", "Velenje"],
        4: ["Brežice", "Črnomelj", "Kočevje", "Krško", "Novo mesto", "Sevnica"],
        5: ["Maribor", "Murska Sobota", "Ormož", "Ptuj", "Slovenska Bistrica"],
    }

    low = raw.lower()
    for _, mesta in obmocje_map.items():
        for city in mesta:
            if city.lower() in low:
                return city
    return None


# -------------------- Parser for new singleton layout --------------------

def _parse_block_node(node) -> Dict:
    """
    Parse a (summary_tr, details_tr) tuple from the new singleton table.
    Returns a dict compatible with your DB/upsert expectations.
    """
    summary_tr, details_tr = node

    def _tx(el):
        return re.sub(r"\s+", " ", (el.get_text(strip=True) if el else "")).strip()

    # date
    date_str = _row_date_str(summary_tr)

    # time (td[data-th="Ura"])
    time_str = None
    for td in summary_tr.select("td"):
        if (td.get("data-th") or "").strip().lower() == "ura":
            time_str = _tx(td)
            break

    # city/obmocje from summary "Tip / Lokacija" cell
    dic = summary_tr.select_one(".contentOpomnik")
    city = _tx(dic.select_one(".dicTitle1")) if dic else ""
    obm_txt = _tx(dic.select_one(".dicDisclaimer")) if dic else ""
    m_zone = re.search(r"Območje\s+(\d+)", obm_txt, re.IGNORECASE)
    obmocje = int(m_zone.group(1)) if m_zone else None

    # details row: full address and exam type
    full_loc = _tx(details_tr.select_one(".dicTitle2")) if details_tr else ""
    location = full_loc or city or None

    # town: prefer details (address), fallback to summary city
    town = _clean_town(full_loc) or _clean_town(city) or None

    # exam type
    details_text = _tx(details_tr) if details_tr else ""
    lt = details_text.lower()
    if "preverjanje znanja vožnje" in lt:
        exam_type = "voznja"
    elif "preverjanje znanja teorije" in lt:
        exam_type = "teorija"
    else:
        st = _tx(summary_tr).lower()
        if "preverjanje znanja vožnje" in st:
            exam_type = "voznja"
        elif "preverjanje znanja teorije" in st:
            exam_type = "teorija"
        else:
            exam_type = None

    # categories (td[data-th="Kategorije"])
    cats = ""
    for td in summary_tr.select("td"):
        if (td.get("data-th") or "").strip().lower() == "kategorije":
            raw = _tx(td)
            parts = [p.strip() for p in raw.split(",")]
            cats = ",".join([p for p in parts if p])
            break

    # places left (td[data-th="Prosta mesta"])
    places_left = None
    for td in summary_tr.select("td"):
        if (td.get("data-th") or "").strip().lower() == "prosta mesta":
            m = re.search(r"\d+", _tx(td))
            places_left = int(m.group(0)) if m else None
            break

    # tolmač mention (allow c/č variants)
    tolmac = bool(re.search(r"tolma[cč]", (_tx(summary_tr) + " " + details_text).lower()))

    date_iso, time_iso = (None, None)
    if date_str and time_str:
        date_iso, time_iso = _parse_iso(date_str, time_str)

    available = bool((places_left or 0) > 0)

    return {
        "date_str": date_str,
        "time_str": time_str,
        "date_iso": date_iso,
        "time_iso": time_iso,
        "obmocje": obmocje,
        "town": town,
        "exam_type": exam_type,
        "places_left": places_left,
        "tolmac": bool(tolmac),
        "categories": cats,
        "location": location,
        "available": available,
    }


# -------------------- Networking --------------------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
def _get(session: httpx.Client, url: str, headers: dict):
    r = session.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r


def _extract_blocks(html: str):
    """
    New layout (singleton): table rows come in pairs:
      - summary:  <tr class="js_dogodekBox js_dicDetailsBtnRow">
      - details:  the immediate next <tr class="js_dicDetails">
    Returns list of (summary_tr, details_tr) tuples.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = soup.select_one("div#results")
    if not results:
        return []
    out = []
    for tr in results.select("table.responsiveTable tr.js_dogodekBox.js_dicDetailsBtnRow"):
        det = tr.find_next_sibling("tr", class_="js_dicDetails")
        out.append((tr, det))
    return out


# -------------------- Main fetcher --------------------

def _localize(dt: datetime) -> datetime:
    """The scraped times are local; attach LOCAL_TZ if naive"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=LOCAL_TZ)


def fetch_all_pages(
    type_: str = "-",
    category: str = "-",
    izpitni_center: str = "-1",
    lokacija: str = "-1",
    max_pages: int = MAX_PAGES,
) -> List[Dict]:
    """
    Crawl paginated AJAX endpoint and return list of slot dicts.
    Stops paginating once we encounter a slot beyond MAX_DAYS_AHEAD.
    """
    default_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "sl-SI,sl;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    cutoff_date = datetime.now(LOCAL_TZ) + timedelta(days=MAX_DAYS_AHEAD)

    with httpx.Client(follow_redirects=True, headers=default_headers) as s:
        # Warmup for cookies
        warm = s.get(MAIN, timeout=20)
        if DEBUG:
            print(f"[warmup] {warm.status_code} cookies={s.cookies}")

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html, */*;q=0.1",
            "Referer": MAIN,
        }

        base = dict(
            lang="si",
            type=type_,
            cat=category,
            izpitniCenter=izpitni_center,
            lokacija=lokacija,
            offset=0,
            sentinel_type="ok",
            sentinel_status="ok",
            is_ajax=1,
            complete="false",
        )

        all_items: List[Dict] = []
        seen = set()
        last_len = None

        for page in range(0, max_pages):
            # singleton works with page=0, keep explicit
            params = {**base, "page": page}
            url = f"{AJAX}?{urlencode(params)}"
            resp = _get(s, url, headers)
            html = resp.text

            human_page = f"page {page}"
            if DEBUG and page <= 1:
                with open(os.path.join(OUTDIR, f"page_{page}.html"), "w", encoding="utf-8") as f:
                    f.write(html)

            if not html or (last_len is not None and len(html) == last_len and len(html) < 100):
                break
            last_len = len(html)

            blocks = _extract_blocks(html)
            if DEBUG:
                print(f"[{human_page}] blocks detected: {len(blocks)}")

            page_new = 0
            stop_due_to_cutoff = False

            for node in blocks:
                info = _parse_block_node(node)
                date = info["date_str"]
                time_str = info["time_str"]

                # require at least date+time
                if not (date and time_str):
                    continue

                # cutoff
                try:
                    dt = datetime.strptime(date.strip(), "%d. %m. %Y")
                    dt = _localize(dt)
                    if dt > cutoff_date:
                        if DEBUG:
                            print(f"[cutoff] hit {date} (> {cutoff_date.date()}), stopping.")
                        stop_due_to_cutoff = True
                        break
                except ValueError:
                    if DEBUG:
                        print(f"[warn] could not parse date: {date!r}")

                # de-dup key
                key = (
                    info["date_str"],
                    info["time_str"],
                    info.get("obmocje"),
                    (info.get("town") or "").strip().lower(),
                    info.get("categories", ""),
                )
                if key in seen:
                    continue
                seen.add(key)

                # enrich and map to the DB shape you posted
                item = dict(info)  # includes date_iso, time_iso, available, location
                item["source_page"] = page

                all_items.append(item)
                page_new += 1

            if stop_due_to_cutoff:
                break

            time.sleep(random.uniform(*REQUEST_PAUSE))

        return all_items