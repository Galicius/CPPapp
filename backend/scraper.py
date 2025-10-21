from __future__ import annotations

import os
import re
import time
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict

import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlencode
from tenacity import retry, stop_after_attempt, wait_exponential

from storage import init_db, upsert_slots


# -------------------- Config --------------------

BASE = "https://e-uprava.gov.si"
MAIN = f"{BASE}/si/javne-evidence/prosti-termini-zemljevid.html?lang=si"
AJAX = f"{BASE}/si/javne-evidence/prosti-termini-zemljevid/content/singleton.html"

MAX_PAGES = 300
MAX_DAYS_AHEAD = 30
REQUEST_PAUSE = (0.6, 1.1)
DEBUG = True

OUTDIR = "debug_pages"
os.makedirs(OUTDIR, exist_ok=True)


# -------------------- Utils --------------------

def _norm_space(s: str) -> str:
    return " ".join(s.replace("\xa0", " ").split())


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


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
    """
    Prefer exact known cities; fallback avoided to prevent false positives.
    """
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
    Returns a dict compatible with your DB updater.
    """
    summary_tr, details_tr = node

    def _tx(el):
        return re.sub(r"\s+", " ", (el.get_text(strip=True) if el else "")).strip()

    # date
    date_str = None
    cal = summary_tr.select_one(".calendarBox")
    if cal and cal.has_attr("aria-label"):
        date_str = cal["aria-label"].strip()
    if not date_str:
        sr = summary_tr.select_one(".calendarBox .sr-only")
        if sr:
            date_str = _tx(sr)

    # time (td[data-th="Ura"])
    time_str = None
    for td in summary_tr.select("td"):
        if (td.get("data-th") or "").strip().lower() == "ura":
            time_str = _tx(td)
            break

    # city/obmocje from summary
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

    # tolmač mention
    tolmac = bool(re.search(r"tolma[cč]", (_tx(summary_tr) + " " + details_text).lower()))

    return {
        "date_str": date_str,
        "time_str": time_str,
        "obmocje": obmocje,
        "town": town,
        "exam_type": exam_type,
        "places_left": places_left,
        "tolmac": bool(tolmac),
        "categories": cats,
        "location": location,
    }


# -------------------- Networking --------------------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
def _get(session: httpx.Client, url: str, headers: dict):
    r = session.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r


def _extract_blocks(html: str):
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

def fetch_all_pages(
    type_: str = "-",
    category: str = "-",
    izpitni_center: str = "-1",
    lokacija: str = "-1",
    max_pages: int = MAX_PAGES,
) -> List[Dict]:
    default_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "sl-SI,sl;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    cutoff_date = datetime.now() + timedelta(days=MAX_DAYS_AHEAD)

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

        for page in range(max_pages):
            # For singleton, page=0 is valid; keep it explicit.
            params = {**base, "page": page}
            url = f"{AJAX}?{urlencode(params)}"
            resp = _get(s, url, headers)
            html = resp.text

            if DEBUG:
                human = f"page {page}"
                print(f"[{human}] status={resp.status_code} len={len(html)} url={url}")
                if page <= 1:
                    os.makedirs(OUTDIR, exist_ok=True)
                    with open(os.path.join(OUTDIR, f"page_{page}.html"), "w", encoding="utf-8") as f:
                        f.write(html)

            if not html or (last_len and len(html) == last_len and len(html) < 100):
                break
            last_len = len(html)

            blocks = _extract_blocks(html)
            if DEBUG:
                print(f"[page {page}] blocks detected: {len(blocks)}")
            if not blocks:
                break

            stop_due_to_cutoff = False
            page_new = 0

            for node in blocks:
                info = _parse_block_node(node)

                # require date+time
                if not (info["date_str"] and info["time_str"]):
                    continue

                # cutoff
                try:
                    dt = datetime.strptime(info["date_str"].strip(), "%d. %m. %Y")
                    if dt > cutoff_date:
                        if DEBUG:
                            print(f"[cutoff] hit {info['date_str']} (> {cutoff_date.date()}), stopping.")
                        stop_due_to_cutoff = True
                        break
                except ValueError:
                    if DEBUG:
                        print(f"[warn] invalid date: {info['date_str']!r}")

                # de-dup across run
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

                date_iso, time_iso = _parse_iso(info["date_str"], info["time_str"])
                available = bool((info.get("places_left") or 0) > 0)

                item = {
                    "date_str": info["date_str"],
                    "time_str": info["time_str"],
                    "date_iso": date_iso,          # <-- match DB shape
                    "time_iso": time_iso,          # <-- match DB shape
                    "obmocje": info["obmocje"],
                    "town": info["town"],
                    "exam_type": info["exam_type"],
                    "places_left": info["places_left"],
                    "tolmac": info["tolmac"],
                    "categories": info.get("categories", ""),
                    "source_page": page,           # <-- match DB shape
                    "location": _compose_location(info.get("obmocje"), info.get("town")),
                    "available": available,        # <-- match DB shape
                }

                all_items.append(item)
                page_new += 1

            if stop_due_to_cutoff:
                break
            if page_new == 0:
                break

            time.sleep(random.uniform(*REQUEST_PAUSE))

        return all_items


# -------------------- CLI entry --------------------

if __name__ == "__main__":
    init_db()
    slots = fetch_all_pages()
    opened, updated = upsert_slots(slots)

    print(f"Found {len(slots)} slots | opened(new): {opened} | touched: {updated}")
    for i, s in enumerate(slots[:5], 1):
        cats = s.get("categories") or "-"
        loc = s.get("location") or "-"
        print(f"{i}. {s['date_str']} {s['time_str']} | {loc} | {cats}")
