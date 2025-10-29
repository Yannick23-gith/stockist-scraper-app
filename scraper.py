# scraper.py
import os
import re
import csv
import json
import time
import math
import logging
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import requests

# -------------------------------------------------------------------
# Logging & DEBUG
# -------------------------------------------------------------------
DEBUG = os.getenv("STOCKIST_DEBUG", "0") == "1"
logger = logging.getLogger("stockist")
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s"))
if not logger.handlers:
    logger.addHandler(handler)

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
PER_PAGE = int(os.getenv("STOCKIST_PER_PAGE", "200"))  # 200 marche souvent sur Stockist

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# -------------------------------------------------------------------
# Utils
# -------------------------------------------------------------------
def _log_debug(msg: str):
    if DEBUG:
        logger.info(f"[stockist] DEBUG: {msg}")

def normalize_stockist_endpoint(ep: str) -> str:
    """
    Quand on capte '.../locations/overview.js', on le convertit en '.../locations.json'
    (l'endpoint qui renvoie réellement les points de vente).
    On gère aussi le cas '.../locations' (sans .json) : on gardera Accept: application/json.
    """
    m = re.search(r"^(https://stockist\.co/api/v1/u\d+/)locations/overview\.js", ep)
    if m:
        base = m.group(1)
        fixed = base + "locations.json"
        _log_debug(f"[NORMALIZE] {ep}  →  {fixed}")
        return fixed

    m2 = re.search(r"^(https://stockist\.co/api/v1/u\d+/)locations$", ep)
    if m2:
        _log_debug(f"[NORMALIZE] garde '{ep}' (endpoint JSON possible)")
        return ep

    return ep

def with_query(url: str, **params) -> str:
    u = urlparse(url)
    q = parse_qs(u.query)
    for k, v in params.items():
        if v is None:
            continue
        q[k] = [str(v)]
    new_q = urlencode({k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in q.items()}, doseq=True)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

# tout en haut de scraper.py (après les imports)
DEFAULT_ACCOUNT = "u20439"  # fallback pour Piece & Love

# ...
STOCKIST_ACCOUNT = os.getenv("STOCKIST_ACCOUNT", "").strip()

# dans scrape_stockist(url):
def scrape_stockist(url: str) -> List[Dict[str, Any]]:
    _log(f"[ENTRY] url={url}")

    # 1) override explicite
    if STOCKIST_ACCOUNT:
        endpoint = build_api_from_account(STOCKIST_ACCOUNT)
        _log(f"[OVERRIDE] STOCKIST_ACCOUNT={STOCKIST_ACCOUNT} → {endpoint}")
        return fetch_locations_from_api(endpoint, url)

    # 1-bis) fallback projet (si l'override n'est pas présent)
    if DEFAULT_ACCOUNT:
        endpoint = build_api_from_account(DEFAULT_ACCOUNT)
        _log(f"[FALLBACK] DEFAULT_ACCOUNT={DEFAULT_ACCOUNT} → {endpoint}")
        return fetch_locations_from_api(endpoint, url)

    # ... le reste (détection HTML/JS) reste identique


def fetch_html(url: str) -> str:
    headers = {"User-Agent": UA}
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text

def normalize_location(item: Dict[str, Any]) -> Dict[str, Any]:
    """Mappe un enregistrement Stockist en ligne CSV standardisée."""
    # Stockist renvoie souvent ces champs (mais ça peut varier un peu selon intégration)
    return {
        "name": item.get("name") or item.get("title") or "",
        "address1": item.get("address1") or item.get("address") or "",
        "address2": item.get("address2") or "",
        "city": item.get("city") or "",
        "region": item.get("region") or item.get("state") or "",
        "postal_code": item.get("postal_code") or item.get("zip") or "",
        "country": item.get("country") or "",
        "phone": item.get("phone") or "",
        "website": item.get("website") or "",
        "lat": item.get("lat") or item.get("latitude") or "",
        "lng": item.get("lng") or item.get("longitude") or "",
    }

# -------------------------------------------------------------------
# Cœur : collecte via l'API Stockist
# -------------------------------------------------------------------
def fetch_locations_from_api(api_url: str, source_url: str) -> List[Dict[str, Any]]:
    """
    Appelle la vraie API de données Stockist (locations.json / locations).
    Paginate jusqu'à la fin. Retourne une liste de dicts normalisés.
    """
    sess = requests.Session()
    all_rows: List[Dict[str, Any]] = []

    api_url = normalize_stockist_endpoint(api_url)

    page = 1
    while True:
        url = with_query(api_url, page=page, per_page=PER_PAGE)
        headers = {
            "Accept": "application/json, */*",
            "User-Agent": UA,
            "Referer": source_url,
            "Origin": f"{urlparse(source_url).scheme}://{urlparse(source_url).netloc}",
        }

        _log_debug(f"[API] GET {url}")
        r = sess.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()

        ctype = (r.headers.get("content-type") or "").lower()

        # Certains renvoient json sans header propre, on tente json sinon parse text
        try:
            data = r.json()
        except json.JSONDecodeError:
            if ctype.startswith("application/json"):
                raise
            # Parfois c'est du JS style "window.XXX = {...}"
            txt = r.text.strip()
            m = re.search(r"=\s*({.*})\s*;?\s*$", txt)
            if m:
                data = json.loads(m.group(1))
            else:
                # Dernier recours
                data = {}

        # Formats fréquents
        items = data.get("locations") or data.get("items")
        if items is None:
            # Certains renvoient directement une liste brute
            if isinstance(data, list):
                items = data
            else:
                items = []

        _log_debug(f"[API] page={page} items={len(items)}")

        for it in items:
            all_rows.append(normalize_location(it))

        # Fin de pagination
        pagination = data.get("pagination") if isinstance(data, dict) else None
        if pagination and isinstance(pagination, dict):
            total_pages = pagination.get("total_pages")
            if total_pages and page >= int(total_pages):
                break
        else:
            # si moins que per_page → dernier lot
            if len(items) < PER_PAGE:
                break

        page += 1

    return all_rows

# -------------------------------------------------------------------
# Scrape principal (appelé par app.py)
# -------------------------------------------------------------------
def scrape_stockist(url: str) -> List[Dict[str, Any]]:
    """
    1) Essaie de trouver l'endpoint Stockist depuis la page (id uXXXXX)
    2) Appelle l'API 'locations(.json)' paginée
    3) Retourne les lignes normalisées
    """
    _log_debug(f"[ENTRY] url={url}")

    html = fetch_html(url)

    # 1) essaie d'extraire directement un endpoint complet dans le HTML
    m = re.search(r"(https://stockist\.co/api/v1/u\d+/locations(?:/overview\.js)?)", html)
    if m:
        endpoint = m.group(1)
        _log_debug(f"[HTML] endpoint détecté → {endpoint}")
    else:
        # 2) sinon, récupère l'id de compte 'uXXXXX' et reconstruis l'URL
        acc = find_stockist_id_in_html(html)
        if not acc:
            raise RuntimeError("Impossible de déterminer le store_id Stockist depuis la page.")
        endpoint = f"https://stockist.co/api/v1/{acc}/locations.json"
        _log_debug(f"[HTML] id détecté → {acc}  → endpoint {endpoint}")

    rows = fetch_locations_from_api(endpoint, url)
    _log_debug(f"[DONE] rows={len(rows)}")
    return rows

# -------------------------------------------------------------------
# (Optionnel) utilitaire pour écrire un CSV local si tu testes en dehors de Flask
# -------------------------------------------------------------------
def write_csv(path: str, rows: List[Dict[str, Any]]):
    fieldnames = [
        "name", "address1", "address2", "city", "region",
        "postal_code", "country", "phone", "website", "lat", "lng"
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

if __name__ == "__main__":
    test_url = os.getenv("TEST_URL", "").strip()
    if not test_url:
        print("Set TEST_URL to a Stockist store-locator page to test.")
    else:
        rs = scrape_stockist(test_url)
        out = os.getenv("OUT", "stores.csv")
        write_csv(out, rs)
        print(f"Wrote {len(rs)} rows to {out}")
