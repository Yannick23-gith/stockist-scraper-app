# scraper.py
from __future__ import annotations

import os
import re
import time
import json
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import requests
from requests.exceptions import RequestException
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeoutError

# ==== Réglages généraux ====
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
REQ_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
NAV_TIMEOUT = int(os.getenv("PLAYWRIGHT_NAV_TIMEOUT", "120000"))  # 120s
PER_PAGE = 200
DEBUG = os.getenv("STOCKIST_DEBUG", "0") == "1"

# Endpoints possibles selon l’intégration
STOCKIST_ENDPOINTS = [
    "https://stockist.co/api/locations",
    "https://stockist.co/api/v2/locations",
    "https://stockist.co/api/v1/locations",
    "https://stockist.co/api/locations/search",
]

# Regex additionnelles pour store_id dans DOM/scripts
STORE_ID_PATS = [
    r'store_id[\'"]?\s*[:=]\s*["\']?(\d+)',
    r'"storeId"\s*:\s*(\d+)',
    r'data-store-id=["\'](\d+)["\']',
    r'Stockist(?:\.init)?\([^)]*storeId[\'"]?\s*[:=]\s*(\d+)',
    r'stockist[^"]+?store_id=(\d+)',
    r'data-stockist-store-id=["\'](\d+)["\']',
    r'data-embed-id=["\']stockist-[^"\']*(\d+)[^"\']*["\']',
]


# ---------- Normalisation ----------
def _normalize_location(loc: Dict[str, Any]) -> Dict[str, Any]:
    def g(*keys, default=""):
        for k in keys:
            v = loc.get(k)
            if v not in (None, ""):
                return v
        return default

    return {
        "name": g("name", "title"),
        "address1": g("address1", "address_1", "street", "line1"),
        "address2": g("address2", "address_2", "line2"),
        "city": g("city", "locality"),
        "region": g("region", "state", "province"),
        "postal_code": g("postal_code", "postcode", "zip"),
        "country": g("country"),
        "phone": g("phone", "telephone", "tel"),
        "website": g("website", "url"),
        "email": g("email"),
        "lat": g("lat", "latitude"),
        "lng": g("lng", "lon", "longitude"),
        "_raw": loc,
    }


def _extract_locations_from_json(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return data  # parfois l'API renvoie directement une liste
    if not isinstance(data, dict):
        return []
    for key in ("locations", "results", "data"):
        if isinstance(data.get(key), list):
            return data[key]  # type: ignore
    return []


def _has_next_page(data: Any, current_page: int) -> bool:
    if isinstance(data, dict):
        for key in ("next_page", "nextPage"):
            if key in data:
                return bool(data[key])
        for key in ("total_pages", "totalPages"):
            if key in data and isinstance(data[key], int):
                return current_page < int(data[key])
        locs = _extract_locations_from_json(data)
        if locs and len(locs) >= PER_PAGE:
            return True
    return False


# ---------- Requêtes HTTP simples ----------
def _safe_get(url: str, headers: Dict[str, str], timeout: float) -> Optional[requests.Response]:
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        return resp
    except RequestException:
        return None


def _extract_store_id_from_text(text: str) -> Optional[str]:
    for pat in STORE_ID_PATS:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(1)
    return None


def _try_extract_store_id_static(url: str) -> Optional[str]:
    headers = {"User-Agent": UA, "Referer": url}
    resp = _safe_get(url, headers, timeout=REQ_TIMEOUT)
    if not resp or resp.status_code >= 400:
        return None
    html = resp.text or ""
    return _extract_store_id_from_text(html)


# ---------- Playwright (cookie + sniff réseau) ----------
COOKIE_SELECTORS = [
    # génériques
    'button:has-text("Tout accepter")',
    'button:has-text("Tout Accepter")',
    'button:has-text("Accepter")',
    'button:has-text("J’accepte")',
    'button:has-text("J\'accepte")',
    'button:has-text("Accept all")',
    'button:has-text("Accept All")',
    'button:has-text("Accept")',
    'button[mode="accept-all"]',
    '[data-testid="uc-accept-all-button"]',
    # Axeptio
    '#axeptio_btn_acceptAll',
    'button.ax-accept-all',
    # OneTrust
    '#onetrust-accept-btn-handler',
    'button#onetrust-accept-btn-handler',
    # Cookiebot
    '#CybotCookiebotDialogBodyButtonAccept',
]

def _click_cookie_banners(page) -> None:
    for sel in COOKIE_SELECTORS:
        try:
            if page.locator(sel).count():
                page.click(sel, timeout=2000)
                page.wait_for_timeout(600)
        except Exception:
            pass


def _extract_store_id_with_playwright_or_capture_api(url: str) -> Dict[str, Any]:
    """
    Retourne l’un des deux:
      - {"store_id": "<id>"} si on a la valeur
      - {"first_chunk": <json>, "endpoint": "<url>", "params": {...}} si on a intercepté la 1ère réponse API
    """
    result: Dict[str, Any] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(user_agent=UA, locale="fr-FR")
        page = context.new_page()

        first_api_json: Optional[Any] = None
        first_api_url: Optional[str] = None

        def on_response(resp):
            nonlocal first_api_json, first_api_url
            if first_api_json is not None:
                return
            u = resp.url
            if "stockist.co/api/" in u and resp.status == 200:
                try:
                    first_api_json = resp.json()
                    first_api_url = u
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except PwTimeoutError:
            pass

        # cookies
        _click_cookie_banners(page)

        # on attend un court instant que les scripts partent
        page.wait_for_timeout(2000)

        if first_api_json is not None and first_api_url:
            result["first_chunk"] = first_api_json
            result["endpoint"] = first_api_url
            # si on peut extraire un store_id depuis l'URL, on le renvoie aussi
            try:
                q = parse_qs(urlparse(first_api_url).query)
                sid = (q.get("store_id") or q.get("storeId") or [""])[0]
                if sid:
                    result["store_id"] = sid
            except Exception:
                pass

        # sinon on tente d’extraire le store_id via DOM/scripts
        if "store_id" not in result:
            try:
                html = page.content()
                sid = _extract_store_id_from_text(html or "")
                if not sid:
                    scripts_texts = page.locator("script").all_text_contents()
                    for s in scripts_texts:
                        sid = _extract_store_id_from_text(s or "")
                        if sid:
                            break
                if sid:
                    result["store_id"] = sid
            except Exception:
                pass

        context.close()
        browser.close()

    return result


# ---------- Fetch API Stockist ----------
def _fetch_all_locations_from_known_endpoint(endpoint_url: str, referer: str) -> List[Dict[str, Any]]:
    """
    endpoint_url = l’URL exacte qu’on a capturée (contient déjà store_id & co).
    On la “rejoue” en paginant avec per=200.
    """
    parsed = urlparse(endpoint_url)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    q = parse_qs(parsed.query)
    # paramètres présents dans l’appel initial
    base_params = {k: (v[0] if isinstance(v, list) else v) for k, v in q.items()}
    if "per" in base_params:
        base_params["per"] = str(PER_PAGE)
    else:
        base_params["per"] = str(PER_PAGE)
    if "page" not in base_params:
        base_params["page"] = "1"

    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "Origin": "https://stockist.co",
    }

    all_locs: List[Dict[str, Any]] = []
    page_num = 1
    while True:
        params = dict(base_params)
        params["page"] = str(page_num)
        try:
            r = requests.get(base, params=params, headers=headers, timeout=REQ_TIMEOUT)
            if r.status_code >= 400:
                break
            data = r.json()
        except Exception:
            break

        locs = _extract_locations_from_json(data)
        if not locs:
            break
        all_locs.extend(locs)
        if not _has_next_page(data, page_num):
            break
        page_num += 1
        time.sleep(0.35)

    return all_locs


def _fetch_all_locations_with_store_id(store_id: str, referer: str) -> List[Dict[str, Any]]:
    all_locations: List[Dict[str, Any]] = []
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "Origin": "https://stockist.co",
    }

    for base in STOCKIST_ENDPOINTS:
        tmp: List[Dict[str, Any]] = []
        page_num = 1
        while True:
            params = {"store_id": store_id, "per": PER_PAGE, "page": page_num}
            try:
                r = requests.get(base, params=params, headers=headers, timeout=REQ_TIMEOUT)
                if r.status_code >= 400:
                    break
                data = r.json()
            except Exception:
                break
            locs = _extract_locations_from_json(data)
            if not locs:
                break
            tmp.extend(locs)
            if not _has_next_page(data, page_num):
                break
            page_num += 1
            time.sleep(0.35)
        if tmp:
            all_locations = tmp
            break

    return all_locations


# ---------- API publique ----------
def scrape_stockist(url: str) -> List[Dict[str, Any]]:
    """
    Scrape un store locator Stockist à l’URL donnée.
    Retourne des lignes normalisées (list[dict]) pour export CSV.
    """
    # 1) d’abord on tente une extraction statique ultra-rapide
    store_id = _try_extract_store_id_static(url)

    # 2) sinon on passe par Playwright : click cookie + sniff réseau
    first_chunk = None
    captured_endpoint = None
    if not store_id:
        captured = _extract_store_id_with_playwright_or_capture_api(url)
        store_id = captured.get("store_id")
        first_chunk = captured.get("first_chunk")
        captured_endpoint = captured.get("endpoint")

    # 3) Si on a déjà une 1ère réponse API, on la rejoue (le plus fiable).
    if first_chunk and captured_endpoint:
        locations = _fetch_all_locations_from_known_endpoint(captured_endpoint, referer=url)
        rows = [_normalize_location(loc) for loc in locations]
        return rows

    # 4) Sinon, si on a un store_id → on tape l’API “classique”.
    if store_id:
        locations = _fetch_all_locations_with_store_id(store_id, referer=url)
        rows = [_normalize_location(loc) for loc in locations]
        return rows

    # 5) Rien trouvé → on lève une erreur claire (c’est ce que voit Flask).
    raise RuntimeError("Impossible de déterminer le store_id Stockist depuis la page.")


# Test local rapide
if __name__ == "__main__":
    test_url = os.getenv("TEST_URL", "").strip()
    if not test_url:
        print("Définis TEST_URL pour tester, ex. :")
        print('TEST_URL="https://pieceandlove.fr/pages/distributeurs" python scraper.py')
    else:
        try:
            res = scrape_stockist(test_url)
            print(f"OK: {len(res)} lieux")
            for r in res[:5]:
                print("-", r["name"], "-", r["city"], r["country"])
        except Exception as e:
            print("Erreur:", e)
            raise
