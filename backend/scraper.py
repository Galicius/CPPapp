from __future__ import annotations

import os
import re
import sys
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


def log(msg: str):
    """Timestamped log to stderr for cloud visibility."""
    ts = datetime.utcnow().isoformat()
    # Cloud Run / Functions will capture this as an info/error log
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


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

def _parse_row_date(tr) -> Optional[str]:
    """Extract date string from a calendar row/cell if present."""
    cal = tr.select_one(".calendarBox")
    if not cal:
        return None
    s = cal.get("aria-label") or _text(cal.select_one(".sr-only")) or ""
    s = s.strip()
    if not s:
        return None
    # Be robust to aria-label like "petek, 24. 10. 2025" — extract the date part.
    m = re.search(r"\b(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\b", s)
    return m.group(1) if m else s


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


def _extract_items_linear(html: str) -> List[Dict]:
    """
    Parse the HTML table linearly to associate rows with their most recent date header.
    Returns a list of dicts (partially filled) ready for final processing.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = soup.select_one("div#results")
    if not results:
        log("No div#results found in HTML")
        if DEBUG:
            log(f"dumping html snippet (first 500 chars): {html[:500]}")
        return []

    items = []
    current_date_str = None
    
    # Select ALL relevant rows in order: headers AND content rows
    # We iterate tr by tr
    table = results.select_one("table.responsiveTable")
    if not table:
         return []

    # Iterate direct children trs to handle structure safely
    # This assumes flat table structure
    rows = table.find_all("tr", recursive=False)
    
    i = 0
    while i < len(rows):
        tr = rows[i]
        
        # 1. Check for date header
        # It might be in this row OR this row acts as date header (calendarBox)
        ds = _parse_row_date(tr)
        if ds:
            current_date_str = ds

        # 2. Check if this is a Summary Row
        if "js_dogodekBox" in tr.get("class", []):
            # It's a summary row.
            # The next row *should* be details (js_dicDetails), but let's verify.
            summary_tr = tr
            details_tr = None
            
            # Look ahead for details
            if i + 1 < len(rows):
                nxt = rows[i+1]
                if "js_dicDetails" in nxt.get("class", []):
                    details_tr = nxt
                    i += 1 # Consume next row
            
            # Now parse the block using the current_date_str
            item = _parse_block_node_linear(summary_tr, details_tr, current_date_str)
            if item:
                items.append(item)
        
        i += 1
        
    return items


def _parse_block_node_linear(summary_tr, details_tr, date_str) -> Optional[Dict]:
    """
    Parse a (summary, details) pair using the pre-resolved date_str.
    """
    def _tx(el):
        return re.sub(r"\s+", " ", (el.get_text(strip=True) if el else "")).strip()

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
    # Log the attempt
    log(f"GET {url}")
    r = session.get(url, headers=headers, timeout=30)
    log(f"GET {url} -> status {r.status_code}")
    r.raise_for_status()
    return r


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
    log(f"START fetch_all_pages config: MAX_PAGES={max_pages}, MAX_DAYS_AHEAD={MAX_DAYS_AHEAD}")

    default_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "sl-SI,sl;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    cutoff_date = datetime.now(LOCAL_TZ) + timedelta(days=MAX_DAYS_AHEAD)
    log(f"Cutoff date: {cutoff_date}")

    try:
        with httpx.Client(follow_redirects=True, headers=default_headers) as s:
            # Warmup for cookies
            log(f"Warmup GET {MAIN}")
            try:
                warm = s.get(MAIN, timeout=20)
                log(f"Warmup status: {warm.status_code}, cookies: {list(s.cookies.keys())}")
            except Exception as e:
                log(f"Warmup failed: {e}")
                # Don't crash, try to proceed? 
                pass

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
                log(f"Fetching page {page}")
                
                # singleton works with page=0, keep explicit
                params = {**base, "page": page}
                url = f"{AJAX}?{urlencode(params)}"
                
                try:
                    resp = _get(s, url, headers)
                except Exception as ex:
                    log(f"Network error on page {page}: {ex}")
                    # If network fails repeatedly, we likely stop
                    break

                html = resp.text
                
                if DEBUG and page <= 1:
                    with open(os.path.join(OUTDIR, f"page_{page}.html"), "w", encoding="utf-8") as f:
                        f.write(html)

                # Check for empty response or identical response
                if not html:
                    log(f"Empty HTML on page {page}, breaking.")
                    break
                
                if last_len is not None and len(html) == last_len:
                    # heuristic: if exact same length, likely same content or empty wrapper
                    if len(html) < 200: # Threshold for "empty" wrapper
                         log(f"Page {page} len={len(html)} same as prev (small), assuming end.")
                         break
                    else:
                        # Log warning in case multiple pages have same size by coincidence
                        log(f"Page {page} len={len(html)} matches previous. Continuing but suspicious.")
                        pass

                last_len = len(html)

                # Parse linearly
                t0 = time.time()
                try:
                    stats_items = _extract_items_linear(html)
                except Exception as pex:
                    log(f"Parse error on page {page}: {pex}")
                    stats_items = []
                
                dur = time.time() - t0
                if DEBUG or dur > 1.0:
                    log(f"Page {page} parsed in {dur:.2f}s, found {len(stats_items)} items.")

                if not stats_items:
                     # sometimes page 1 has no blocks if really empty, but if page 0 had blocks and this doesn't...
                     # log(f"No blocks found on page {page}.")
                     pass

                page_new = 0
                stop_due_to_cutoff = False

                for info in stats_items:
                    date = info["date_str"]
                    time_str = info["time_str"]

                    # require at least date+time
                    if not (date and time_str):
                        # log(f"Skipping block without date/time. Raw: {info}")
                        continue

                    # cutoff
                    try:
                        dt = datetime.strptime(date.strip(), "%d. %m. %Y")
                        dt = _localize(dt)
                        if dt > cutoff_date:
                            log(f"[cutoff] hit {date} (> {cutoff_date.date()}), stopping.")
                            stop_due_to_cutoff = True
                            break
                    except ValueError:
                         log(f"[warn] could not parse date: {date!r}")

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

                log(f"Page {page}: found {len(stats_items)} blocks, {page_new} new items.")

                if stop_due_to_cutoff:
                    break
                
                # Heuristic: if valid page but 0 items found? Could be end of list but not empty HTML.
                if page > 0 and len(stats_items) == 0:
                    log(f"Page {page} has 0 blocks, assuming end of pagination.")
                    break

                time.sleep(random.uniform(*REQUEST_PAUSE))
            
            log(f"END fetch_all_pages. Total items: {len(all_items)}")
            return all_items

    except Exception as e:
        log(f"CRITICAL in fetch_all_pages: {e}")
        raise e