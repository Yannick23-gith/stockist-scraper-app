# scraper.py
from __future__ import annotations

import os
import re
import time
import json
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import RequestException

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeoutError


# ==== Réglages ====
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
REQ_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
NAV_TIMEOUT = int(os.getenv("PLAYWRIGHT_NAV_TIMEOUT", "120000"))  # 120s pour Render Free
PER_PAGE = 200  # on récupère large pour réduire la pagination


# ==== Helpers ====

# Regex pour repérer le store_id dans le HTML / scripts
_STOCKIST_ID_PATTERNS = [
    r'store_id[\'"]?\s*[:=]\s*["\']?(\d+)',          # store_id: 12345 / "store_id": "12345"
    r'"storeId"\s*:\s*(\d+)',                       # "storeId": 12345
    r'data-store-id=["\'](\d+)["\']',               # data-store-id="12345"
    r'Stockist(?:\.init)?\([^)]*storeId[\'"]?\s*[:=]\s*(\d+)',  # Stockist.init({ storeId: 12345 })
    r'stockist[^"]+?store_id=(\d+)',                # URLs avec ?store_id=12345
]

# Plusieurs endpoints possibles suivant l’intégration
_STOCKIST_ENDPOINTS = [
    # format le plus courant :
    "https://stockist.co/api/locations",
    "https://stockist.co/api/v2/locations",
    "https://stockist.co/api/v1/locations",
    # backup fréquent :
    "https://stockist.co/api/locations/search",
]


def _extract_store_id_from_text(text: str) -> Optional[str]:
    for pat in _STOCKIST_ID_PATTERNS:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(1)
    return None


def _safe_get(url: str, headers: Dict[str, str], timeout: float) -> Optional[requests.Response]:
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        # Parfois 403/401 → on laisse remonter pour tester d'autres endpoints
        if resp.status_code >= 400:
            return resp  # on renverra quand même pour diagnostiquer
        return resp
    except RequestException:
        return None


def _try_extract_store_id_static(url: str) -> Optional[str]:
    """
    Premier essai : on récupère le HTML via requests et on scanne.
    """
    headers = {"User-Agent": UA, "Referer": url}
    resp = _safe_get(url, headers, timeout=REQ_TIMEOUT)
    if not resp or resp.status_code >= 400:
        return None
    html = resp.text or ""
    return _extract_store_id_from_text(html)


def _extract_store_id_with_playwright(url: str) -> Optional[str]:
    """
    Fallback Playwright : navigate sans bloquer sur networkidle, puis cherche le store_id
    dans : DOM, scripts, et trafic réseau (requêtes contenant store_id).
    """
    found_store_id: Optional[str] = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            locale="fr-FR",
            user_agent=UA
        )
        page = context.new_page()

        # capture du trafic sortant pour y lire store_id
        def _on_request(req):
            nonlocal found_store_id
            if found_store_id:
                return
            u = req.url
            m = re.search(r"store_id=(\d+)", u)
            if m:
                found_store_id = m.group(1)

        page.on("request", _on_request)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except PwTimeoutError:
            # On continue quand même : on aura souvent assez de DOM pour parser
            pass

        # 1) DOM
        if not found_store_id:
            html = page.content()
            found_store_id = _extract_store_id_from_text(html)

        # 2) scripts inline
        if not found_store_id:
            try:
                scripts_texts = page.locator("script").all_text_contents()
                for s in scripts_texts:
                    sid = _extract_store_id_from_text(s or "")
                    if sid:
                        found_store_id = sid
                        break
            except Exception:
                pass

        context.close()
        browser.close()

    return found_store_id


def _extract_store_id(url: str) -> str:
    """
    Tente d'extraire le store_id (statique puis Playwright).
    """
    store_id = _try_extract_store_id_static(url)
    if store_id:
        return store_id

    store_id = _extract_store_id_with_playwright(url)
    if store_id:
        return store_id

    raise RuntimeError("Impossible de déterminer le store_id Stockist depuis la page.")


def _normalize_location(loc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Harmonise les champs les plus utiles en sortie CSV.
    On garde les clés principales (nom, adresse, téléphone, etc.).
    """
    def g(*keys, default=""):
        for k in keys:
            if k in loc and loc[k] is not None:
                return loc[k]
        return default

    # Les clés varient selon les versions d’API ; on couvre l’essentiel
    name = g("name", "title")
    phone = g("phone", "telephone", "tel")
    website = g("website", "url")
    email = g("email")
    lat = g("lat", "latitude")
    lng = g("lng", "lon", "longitude")

    # Adresse
    address1 = g("address1", "address_1", "street", "line1")
    address2 = g("address2", "address_2", "line2")
    city = g("city", "locality")
    region = g("region", "state", "province")
    postal = g("postal_code", "postcode", "zip")
    country = g("country")

    return {
        "name": name,
        "address1": address1,
        "address2": address2,
        "city": city,
        "region": region,
        "postal_code": postal,
        "country": country,
        "phone": phone,
        "website": website,
        "email": email,
        "lat": lat,
        "lng": lng,
        # on laisse aussi l'objet brut si tu veux debugger ensuite
        "_raw": loc,
    }


def _try_fetch_page(endpoint_base: str, store_id: str, page: int, referer: str) -> Optional[Dict[str, Any]]:
    """
    Essaye un endpoint pour une page donnée et renvoie le JSON (ou None si KO).
    """
    params = {
        "store_id": store_id,
        "page": page,
        "per": PER_PAGE,
    }
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "Origin": "https://stockist.co",
    }

    try:
        resp = requests.get(endpoint_base, params=params, headers=headers, timeout=REQ_TIMEOUT)
        if resp.status_code >= 400:
            return None
        return resp.json()
    except Exception:
        return None


def _extract_locations_from_json(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Différentes formes possibles suivant la version : locations / results / data...
    """
    if not data:
        return []
    for key in ("locations", "results", "data"):
        if isinstance(data.get(key), list):
            return data[key]  # type: ignore
    # parfois la racine est déjà une liste
    if isinstance(data, list):
        return data  # type: ignore
    return []


def _has_next_page(data: Dict[str, Any], current_page: int) -> bool:
    """
    Devine s'il y a une pagination restante en fonction des champs disponibles.
    """
    # cas 1 : champs classiques
    for key in ("next_page", "nextPage"):
        if key in data:
            return bool(data[key])

    # cas 2 : total_pages
    for key in ("total_pages", "totalPages"):
        if key in data and isinstance(data[key], int):
            return current_page < int(data[key])

    # cas 3 : s'il y a moins que PER_PAGE, on stoppe
    locations = _extract_locations_from_json(data)
    if locations and len(locations) >= PER_PAGE:
        return True

    return False


def _fetch_all_locations(store_id: str, referer: str) -> List[Dict[str, Any]]:
    """
    Essaie les endpoints connus de Stockist, pagine, et renvoie la liste brute.
    """
    all_locations: List[Dict[str, Any]] = []

    for endpoint in _STOCKIST_ENDPOINTS:
        try:
            page_num = 1
            tmp: List[Dict[str, Any]] = []
            while True:
                data = _try_fetch_page(endpoint, store_id, page=page_num, referer=referer)
                if not data:
                    break
                locations = _extract_locations_from_json(data)
                if not locations:
                    break
                tmp.extend(locations)
                if not _has_next_page(data, page_num):
                    break
                page_num += 1
                # léger sleep pour éviter d'agresser l'API
                time.sleep(0.35)

            # si on a trouvé au moins quelque chose, on l'utilise et on quitte
            if tmp:
                all_locations = tmp
                break
        except Exception:
            # en cas d'échec, on tente l'endpoint suivant
            continue

    return all_locations


# ====== FONCTION PRINCIPALE ======
def scrape_stockist(url: str) -> List[Dict[str, Any]]:
    """
    Récupère tous les magasins du store locator Stockist présent à `url`.
    Retourne une liste de dictionnaires normalisés (prêts pour CSV).
    """
    # 1) obtention du store_id (statique -> playwright)
    store_id = _extract_store_id(url)

    # 2) fetch des magasins via API Stockist (plus fiable que parser la page)
    raw_locations = _fetch_all_locations(store_id, referer=url)

    # 3) normalisation des lignes
    rows = [_normalize_location(loc) for loc in raw_locations]
    return rows


# === Test rapide en local ===
if __name__ == "__main__":
    test_url = os.getenv("TEST_URL", "").strip()
    if not test_url:
        print("⚠️  Définis TEST_URL pour tester, ex:")
        print('TEST_URL="https://pieceandlove.fr/pages/distributeurs" python scraper.py')
        raise SystemExit(0)

    try:
        res = scrape_stockist(test_url)
        print(f"OK: {len(res)} magasins trouvés")
        # petit aperçu
        for r in res[:5]:
            print(f"- {r['name']} – {r['city']} {r['country']}")
    except Exception as e:
        print("Erreur:", e)
        raise
