# scraper.py
import re
import time
import json
import logging
import urllib.parse as urlparse
from typing import List, Dict, Any, Optional

import requests

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Deux variantes d'API qu'on rencontre chez Stockist
ENDPOINTS = [
    "https://app.stockist.co/api/stores/{id}/locations",
    "https://api.stockist.co/v1/stores/{id}/locations",
]

logger = logging.getLogger("scraper")
logger.setLevel(logging.INFO)


def _http_get(url: str, referer: Optional[str] = None, params: Optional[dict] = None) -> requests.Response:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
    }
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = urlparse.urlsplit(referer).scheme + "://" + urlparse.urlsplit(referer).netloc

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    return resp


def _is_digits(s: str) -> bool:
    return s.isdigit() and len(s) >= 3


def _extract_store_id_from_html(html: str) -> Optional[str]:
    """
    Essaie plusieurs patterns pour extraire le store_id à partir du HTML.
    """
    patterns = [
        r'widget(?:\.min)?\.js[^"]*?[\?&]store=(\d+)',
        r'[?&]store=(\d+)',                     # fallback très large
        r'"store"\s*:\s*(\d+)',                 # JSON inline
        r"'store'\s*:\s*(\d+)",
        r'data-store-id=["\'](\d+)["\']',
        r'data-store=["\'](\d+)["\']',
        r'StockistSettings\s*=\s*{[^}]*"store"\s*:\s*(\d+)',
        r'Stockist\s*=\s*{[^}]*"store"\s*:\s*(\d+)',
    ]
    for pat in patterns:
        m = re.search(pat, html, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _extract_store_id(input_value: str) -> (str, Optional[str]):
    """
    Retourne (store_id, referer) à partir d'une URL de store locator OU d'un store_id.
    """
    s = input_value.strip()
    if _is_digits(s):
        return s, None

    # Si on nous passe une URL de la forme ?store=12345 dans la query string, récupère directement
    qs_id = None
    try:
        parsed = urlparse.urlsplit(s)
        qs = urlparse.parse_qs(parsed.query)
        if "store" in qs and qs["store"] and _is_digits(qs["store"][0]):
            qs_id = qs["store"][0]
    except Exception:
        pass

    if qs_id:
        return qs_id, s

    # Sinon, on télécharge le HTML pour détecter l'ID
    logger.info("[SCRAPER] Fetch page to extract store_id: %s", s)
    resp = _http_get(s, referer=s)
    if not resp.ok:
        raise RuntimeError(f"Impossible de charger la page ({resp.status_code}).")

    store_id = _extract_store_id_from_html(resp.text)
    if not store_id:
        raise RuntimeError("Impossible de déterminer le store_id Stockist depuis la page.")

    logger.info("[SCRAPER] store_id=%s", store_id)
    return store_id, s


def _normalize_item(it: Dict[str, Any]) -> Dict[str, Any]:
    def get(*keys):
        for k in keys:
            if k in it and it[k] is not None:
                return it[k]
        return ""

    # Certains champs diffèrent selon versions d'API
    name = get("name", "title")
    phone = get("phone", "telephone")
    website = get("website", "url", "link")
    lat = get("lat", "latitude")
    lng = get("lng", "longitude")
    address_full = get("address_string", "address_full", "formatted_address")

    address1 = get("address1", "address_1", "address_first_line", "address")
    address2 = get("address2", "address_2", "address_second_line")
    city = get("city", "locality", "town")
    state = get("state", "region", "province")
    postal_code = get("postal_code", "postcode", "zip")
    country = get("country", "country_code", "country_name")

    return {
        "name": name,
        "address1": address1,
        "address2": address2,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "country": country,
        "phone": phone,
        "website": website,
        "lat": lat,
        "lng": lng,
        "address_full": address_full,
    }


def _fetch_all_locations(store_id: str, referer: Optional[str]) -> List[Dict[str, Any]]:
    """
    Essaye les différents endpoints + pagination.
    """
    per_page = 100
    collected: List[Dict[str, Any]] = []

    for base in ENDPOINTS:
        url_base = base.format(id=store_id)
        page = 1
        collected = []

        logger.info("[SCRAPER] Try endpoint: %s", url_base)

        while True:
            params = {"per_page": per_page, "page": page}
            resp = _http_get(url_base, referer=referer or f"https://stocki.st/{store_id}", params=params)

            # Gère throttling basique
            if resp.status_code in (429, 503):
                time.sleep(1.5)
                continue

            if not resp.ok:
                logger.info("[SCRAPER] %s -> %s", resp.url, resp.status_code)
                break

            try:
                data = resp.json()
            except json.JSONDecodeError:
                logger.info("[SCRAPER] JSON decode failed on page=%s", page)
                break

            # Les données peuvent être dans keys différentes
            items = None
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("locations") or data.get("results") or data.get("data")
                if items is None and "locations" in data:
                    items = data["locations"]

            if not items:
                if page == 1:
                    logger.info("[SCRAPER] Empty payload on first page.")
                break

            logger.info("[SCRAPER] page=%s -> %s items", page, len(items))
            collected.extend(items)

            if len(items) < per_page:
                break
            page += 1

        if collected:
            # Si on a trouvé sur un endpoint, on sort
            break

    return collected


def scrape_stockist(input_value: str) -> List[Dict[str, Any]]:
    """
    Point d'entrée appelé par app.py
    - input_value : URL du store locator OU store_id numérique
    - retourne une liste de dicts normalisés
    """
    store_id, referer = _extract_store_id(input_value)
    raw_items = _fetch_all_locations(store_id, referer)

    if not raw_items:
        raise RuntimeError(
            "Aucun point de vente renvoyé par l’API Stockist. "
            "Vérifie le store_id et que la page utilise bien Stockist."
        )

    rows = [_normalize_item(it) for it in raw_items]
    logger.info("[SCRAPER] total rows = %s", len(rows))
    return rows
