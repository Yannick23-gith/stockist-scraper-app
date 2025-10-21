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


import re
import requests

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

def _extract_store_id(input_value: str) -> tuple[str, str | None]:
    """
    Retourne (store_id, referer).
    - Si l'entrée est déjà un ID numérique -> on le renvoie direct.
    - Sinon on télécharge la page et on cherche le store_id dans plein d'endroits possibles.
    """
    input_value = (input_value or "").strip()

    # 1) Si c’est déjà un ID (ex: "53141635251")
    if re.fullmatch(r"\d{5,}", input_value):
        return input_value, None

    # 2) Sinon, on considère que c'est une URL — on récupère l'HTML
    try:
        resp = requests.get(input_value, headers=UA, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Impossible de charger l'URL fournie : {e}")

    html = resp.text

    # 3) Tous les patterns plausibles rencontrés sur les intégrations Stockist
    patterns = [
        # Attributs data-*
        r'data-stockist-store=["\'](\d+)["\']',
        r'data-stockist_store_id=["\'](\d+)["\']',
        r'data-store-id=["\'](\d+)["\']',

        # Variables JS usuelles
        r'stockist_store_id\s*[:=]\s*["\']?(\d+)',
        r'storeId\s*[:=]\s*["\']?(\d+)',
        r'stockist\s*=\s*{[^}]*store_id\s*:\s*(\d+)',
        r'stockistSettings\s*=\s*{[^}]*store_id\s*:\s*(\d+)',
        r'window\.stockistConfig\s*=\s*{[^}]*store_id\s*[:=]\s*["\']?(\d+)',

        # URLs d’API/iframe/script où l’ID est présent
        r'stockist-api\.stockist\.co/[^"\']*?/(\d+)/locations',
        r'stockist\.co/[^"\']*?/(\d+)/locations',
        r'stockist\.co/[^"\']*?store_id=(\d+)',
        r'storelocator\.stockist\.co/[^"\']*?store_id=(\d+)',
        r'embed\.js[^"\']*?(?:\?|&)store_id=(\d+)',
    ]

    for pat in patterns:
        m = re.search(pat, html, re.I | re.S)
        if m:
            store_id = m.group(1)
            return store_id, input_value  # referer = la page d’origine

    # 4) Dernier recours : regarder tous les <script src="…"> et iframes
    #    -> parfois l’ID n’est visible que dans l’URL des assets
    m = re.search(r'<(?:script|iframe)[^>]+src=["\'][^"\']*(?:store_id|storeId)=(\d+)', html, re.I)
    if m:
        return m.group(1), input_value

    raise RuntimeError("Impossible de déterminer le store_id Stockist depuis la page.")



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
