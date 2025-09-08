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
MAIN = f"{BASE}/javne-evidence/prosti-termini.html?lang=si"
AJAX = f"{BASE}/si/javne-evidence/prosti-termini/content/singleton.html"

MAX_PAGES = 300               # hard safety cap
MAX_DAYS_AHEAD = 30           # stop when a slot's date is beyond this many days
REQUEST_PAUSE = (0.6, 1.1)    # random sleep range between pages (seconds)
DEBUG = True

OUTDIR = "debug_pages"
os.makedirs(OUTDIR, exist_ok=True)


# -------------------- Known towns / canonicalization --------------------

# Canonical names for towns, including variants that appear on the site
# (e.g., "POSTOJNA EPIC" -> "POSTOJNA", "PTUJ CCE KAT" -> "PTUJ").
KNOWN_TOWNS_BY_ZONE = {
    1: [
        "AJDOVŠČINA", "IDRIJA", "ILIRSKA BISTRICA", "KOPER",
        "NOVA GORICA", "POSTOJNA", "SEŽANA", "TOLMIN",
    ],
    2: [
        "DOMŽALE", "IG", "JESENICE", "KRANJ", "LJUBLJANA", "VRHNIKA",
    ],
    3: [
        "CELJE", "LAŠKO", "LOČICA OB SAVINJI", "RAVNE NA KOROŠKEM",
        "SLOVENSKE KONJICE", "SLOVENJ GRADEC", "ŠENTJUR",
        "ŠMARJE PRI JELŠAH", "TRBOVLJE", "VELENJE",
    ],
    4: [
        "BREŽICE", "ČRNOMELJ", "KOČEVJE", "KRŠKO",
        "NOVO MESTO", "SEVNICA",
    ],
    5: [
        "MARIBOR", "MURSKA SOBOTA", "ORMOŽ", "PTUJ", "SLOVENSKA BISTRICA",
    ],
}

# Patterns that should be reduced to a canonical town
_CANONICAL_VARIANTS = {
    # zone 1
    "POSTOJNA EPIC": "POSTOJNA",
    "TOLMIN IIN": "TOLMIN",
    "TOLMIN UE": "TOLMIN",
    # zone 4
    "BREŽICE TEORIJA": "BREŽICE",
    "KOČEVJE ŠD GAJ": "KOČEVJE",
    "KOČEVJE VADBENA POVRŠINA HERBBY": "KOČEVJE",
    "NOVO MESTO BTC ČEŠČA VAS BE CE": "NOVO MESTO",
    "NOVO MESTO UE": "NOVO MESTO",
    # zone 5
    "PTUJ CCE KAT": "PTUJ",
    "PTUJ KAT": "PTUJ",
}


def _canonicalize_town(raw: str, obmocje: int | None) -> str | None:
    """Return a canonical town name if we can confidently match it.
    Strategy:
      1) Apply explicit variants mapping.
      2) If obmocje is known, look for any known town for that zone in the text.
      3) Fallback to uppercase-only heuristic (first words before address).
    """
    if not raw:
        return None

    # 1) Direct variants
    up = raw.upper()
    for variant, canon in _CANONICAL_VARIANTS.items():
        if variant in up:
            return canon

    # 2) Match against known towns for the detected zone
    if obmocje in KNOWN_TOWNS_BY_ZONE:
        for town in KNOWN_TOWNS_BY_ZONE[obmocje]:
            # word-boundary-ish check; allow commas/spaces before/after
            if re.search(rf"(?<!\\w){re.escape(town)}(?!\\w)", up):
                return town

    # 3) Fallback: heuristic uppercase extraction
    return _clean_town(up)


# -------------------- Utils --------------------

def _norm_space(s: str) -> str:
    return " ".join(s.replace("\xa0", " ").split())


def _soup(html: str) -> BeautifulSoup:
    # Prefer lxml if installed; fallback otherwise.
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def _compose_location(obmocje: Optional[int], town: Optional[str]) -> Optional[str]:
    """
    Back-compat display string (storage used it previously).
    """
    if obmocje is None and not town:
        return None
    if obmocje is not None and town:
        return f"Območje {obmocje} , {town}"
    if obmocje is not None:
        return f"Območje {obmocje}"
    return town


# -------------------- Field parsers --------------------

def _parse_places_left(node) -> Optional[int]:
    """
    Reads the green 'Še X prosto/prosti/prostih ...' banner.
    """
    banner = node.select_one("div.contentOpomnik .lessImportant.green")
    if not banner:
        return None
    txt = _norm_space(banner.get_text(" ", strip=True))
    # e.g. "Še 1 prosto mesto" / "Še 2 prosti mesti" / "Še 5 prostih mest"
    m = re.search(r"Še\s+(\d+)\s+", txt, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_exam_type(node) -> Optional[str]:
    """
    'Preverjanje znanja vožnje'  -> 'voznja'
    'Preverjanje znanja teorije' -> 'teorija'
    Also recognizes short occurrences of 'vožnja' / 'teorija' elsewhere in content.
    """
    co = node.select_one("div.contentOpomnik")
    if not co:
        return None
    t = _text(co).lower()
    if "preverjanje znanja vožnje" in t or re.search(r"\\bvožnja\\b|\\bvoznja\\b", t):
        return "voznja"
    if "preverjanje znanja teorije" in t or re.search(r"\\bteorija\\b", t):
        return "teorija"
    return None
    """
    'Preverjanje znanja vožnje'  -> 'voznja'
    'Preverjanje znanja teorije' -> 'teorija'
    """
    co = node.select_one("div.contentOpomnik")
    if not co:
        return None
    t = _text(co).lower()
    if "preverjanje znanja vožnje" in t:
        return "voznja"
    if "preverjanje znanja teorije" in t:
        return "teorija"
    return None


def _clean_town(raw: str) -> Optional[str]:
    """
    Keep only the town/city name in CAPS (can be multiple words), stop before
    address/keywords/digits/lowercase.

    Examples:
      'BREŽICE Izobraževalno vadbeni center Bizeljska cesta' -> 'BREŽICE'
      'MARIBOR, Cesta k Tamu 11'                             -> 'MARIBOR'
      'NOVA GORICA Kidričeva 9'                              -> 'NOVA GORICA'
    """
    raw = raw.replace(",", " ")
    tokens = [t for t in raw.split() if t]

    parts: List[str] = []
    for tok in tokens:
        # Stop when digits appear (address numbers)
        if re.match(r"^\d", tok):
            break
        # Stop if token starts with lowercase (likely address word)
        if tok[0].islower():
            break
        # Stop on common street/location words (case-insensitive)
        if tok.lower() in {"ulica", "cesta", "naselje", "center", "trg", "testirnica", "vožnja", "voznja"}:
            break
        # Accept token if it's uppercase-ish (allow Slovenian diacritics & hyphens)
        # Heuristic: consider letters that are not lowercase (digits and punctuation already filtered)
        if tok.upper() == tok:
            parts.append(tok)
        else:
            # Mixed-case => likely not part of town name
            break

    town = " ".join(parts).strip(" .,")

    return town or None


def _parse_obmocje_and_town(node) -> Tuple[Optional[int], Optional[str], bool]:
    """
    From 'upperOpomnikDiv' line extract:
      - obmocje (int from 'Območje X')
      - town    (canonical city name; not necessarily all-caps in source)
      - tolmac  (True if 'tolmač' appears)

    Also supports patterns like: 'Za izpit s tolmačem MARIBOR, ...' where we can
    directly capture the town.
    """
    content = node.select_one("div.contentOpomnik")
    co_text = _norm_space(_text(content)) if content else ""

    # First, try 'Za izpit s tolmačem TOWN' pattern anywhere in the card
    m_tol = re.search(r"Za\\s+izpit\\s+s\\s+tolma[cč]em\\s+([A-Za-zČŠŽĆĐčšžćđ\-\\s]+)", co_text, re.IGNORECASE)
    tolmac = bool(m_tol)

    tolmac_town = None
    if m_tol:
        # stop before a comma or known address keywords
        cand = m_tol.group(1).strip()
        cand = re.split(r",|\\s+ulica|\\s+cesta|\\s+naselje|\\s+center|\\s+trg|\\s+testirnica", cand, flags=re.IGNORECASE)[0]
        tolmac_town = cand.strip()

    # Now parse the standard 'Območje X, ...' line
    upper = node.select_one("div.contentOpomnik div.upperOpomnikDiv")
    raw_upper = _norm_space(_text(upper)) if upper else ""

    # Območje
    m_zone = re.search(r"Območje\\s+(\\d+)", raw_upper, re.IGNORECASE)
    obmocje = int(m_zone.group(1)) if m_zone else None

    # Candidate town region from the standard line: text after first comma
    after = raw_upper.split(",", 1)[1].strip() if "," in raw_upper else raw_upper

    # Prefer tolmač-captured town if present; otherwise canonicalize from upper line
    town = _canonicalize_town(tolmac_town or after, obmocje)

    return obmocje, town, tolmac


def _normalize_categories(text: str) -> List[str]:
    cats: List[str] = []
    if "Kategorije:" in text:
        after = text.split("Kategorije:", 1)[1]
        for tok in after.replace(",", " ").split():
            t = tok.strip(" ,;/|")
            if t and len(t) <= 3:  # A, A2, B1, G, F ...
                cats.append(t)
    return cats


def _parse_block_node(node) -> Dict:
    """
    Parse a single <div class="js_dogodekBox dogodek"> card into a dict.
    """
    # Date from calendar box
    date_str: Optional[str] = None
    cal = node.select_one("div.calendarBox")
    if cal and cal.has_attr("aria-label"):
        date_str = cal["aria-label"].strip()
    if not date_str:
        sr = node.select_one("div.calendarBox .sr-only")
        if sr:
            date_str = _text(sr)

    # Time from 'Začetek ob <span class="bold">HH:MM</span>'
    time_str: Optional[str] = None
    for d in node.select("div.contentOpomnik > div"):
        txt = _text(d)
        if "Začetek ob" in txt:
            b = d.select_one("span.bold")
            if b:
                time_str = _text(b)
            else:
                m = re.search(r"\b(\d{1,2}:\d{2})\b", txt)
                if m:
                    time_str = m.group(1)
            break

    # Categories
    categories: List[str] = []
    for d in node.select("div.contentOpomnik > div"):
        if "Kategorije:" in _text(d):
            for sp in d.select("span.bold"):
                t = _text(sp).rstrip(",")
                if t:
                    categories.append(t)
            break

    # Places left (green box)
    places_left = _parse_places_left(node)

    # Exam type
    exam_type = _parse_exam_type(node)

    # Območje + Town + Tolmač (from line containing 'Območje X, <town> ...')
    obmocje, town, tolmac_from_line = _parse_obmocje_and_town(node)

    # Additional tolmac check anywhere inside content block
    co = node.select_one("div.contentOpomnik")
    tolmac_anywhere = ("tolmač" in _text(co).lower()) if co else False
    tolmac = bool(tolmac_from_line or tolmac_anywhere)

    # Build record
    return {
        "date_str": date_str,
        "time_str": time_str,
        "obmocje": obmocje,
        "town": town,
        "exam_type": exam_type,
        "places_left": places_left,
        "tolmac": tolmac,
        "categories": ",".join(categories),
    }


# -------------------- Networking --------------------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
def _get(session: httpx.Client, url: str, headers: dict):
    r = session.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r


def _extract_blocks(html: str):
    """
    Return a list of <div class="js_dogodekBox dogodek"> nodes.
    """
    soup = _soup(html)
    return soup.select("div.dogodki div#results div.js_dogodekBox.dogodek")


# -------------------- Main fetcher --------------------

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

    cutoff_date = datetime.now() + timedelta(days=MAX_DAYS_AHEAD)

    with httpx.Client(follow_redirects=True, headers=default_headers) as s:
        # Warmup for cookies
        warm = s.get(MAIN, timeout=20)
        if DEBUG:
            print(f"[warmup] {warm.status_code} cookies={s.cookies}")

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": MAIN,
            "Accept": "text/html, */*;q=0.01",
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

        for page in range(1, max_pages + 1):
            params = {**base, "page": page}
            url = f"{AJAX}?{urlencode(params)}"
            resp = _get(s, url, headers)
            html = resp.text

            if DEBUG:
                print(f"[page {page}] status={resp.status_code} len={len(html)} url={url}")
                if page <= 2:
                    with open(os.path.join(OUTDIR, f"page_{page}.html"), "w", encoding="utf-8") as f:
                        f.write(html)

            if not html or (last_len is not None and len(html) == last_len and len(html) < 100):
                break
            last_len = len(html)

            blocks = _extract_blocks(html)
            if DEBUG:
                print(f"[page {page}] blocks detected: {len(blocks)}")
            if not blocks:
                break

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

                # back-compat 'location' for storage/printing
                location_str = _compose_location(info.get("obmocje"), info.get("town"))

                all_items.append({
                    "date_str": info["date_str"],
                    "time_str": info["time_str"],
                    "obmocje": info["obmocje"],
                    "town": info["town"],
                    "exam_type": info["exam_type"],
                    "places_left": info["places_left"],
                    "tolmac": info["tolmac"],
                    "categories": info.get("categories", ""),
                    "location": location_str,      # <- keep for storage compatibility
                    "source_page": page,
                })
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
