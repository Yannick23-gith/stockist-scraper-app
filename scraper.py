import os
import re
import csv
import json
import time
import typing as t
from dataclasses import dataclass

import requests


# -----------------------------
# Configuration / Debug helpers
# -----------------------------

STOCKIST_DEBUG = os.getenv("STOCKIST_DEBUG", "0") not in ("0", "", "false", "False", "FALSE")
STOCKIST_ACCOUNT_ENV = os.getenv("STOCKIST_ACCOUNT", "").strip()

# Fallback projet pour Piece & Love (tu peux le laisser ou le vider)
DEFAULT_ACCOUNT = "u20439"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 30
PER_PAGE = 200


def _log(msg: str) -> None:
    if STOCKIST_DEBUG:
        print(f"[stockist] DEBUG: {msg}", flush=True)


# -----------------------------
# Utilitaires Stockist
# -----------------------------

def build_api_endpoint(account: str, page: int) -> str:
    """
    Construit l'endpoint JSON paginé.
    """
    return f"https://stockist.co/api/v1/{account}/locations.json?page={page}&per_page={PER_PAGE}"


def guess_overview_script(account: str) -> str:
    """
    Script 'overview.js' parfois visible dans le HTML ; utile en debug.
    """
    return f"https://stockist.co/api/v1/{account}/locations/overview.js"


# -----------------------------
# Détection de l'ID dans le HTML
# -----------------------------

# Quelques patrons fréquemment vus dans les intégrations Stockist
_PATTERNS = [
    re.compile(r"https?://stockist\.co/api/v1/(u\d+)/locations", re.I),
    re.compile(r'"account_id"\s*:\s*"(u\d+)"', re.I),
    re.compile(r"data-account\s*=\s*\"(u\d+)\"", re.I),
    re.compile(r"data-stockist-account\s*=\s*\"(u\d+)\"", re.I),
]


def find_stockist_id_in_html(html: str) -> t.Optional[str]:
    """
    Essaie de retrouver l'ID de compte Stockist (format u12345) dans le HTML.
    """
    if not html:
        return None
    for pat in _PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None


def fetch_html(url: str) -> str:
    """
    Récupération simple du HTML (sans JS) — suffisant pour beaucoup d'intégrations.
    """
    _log(f"[STATIC] GET {url}")
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    # Encodage très variable sur Shopify/WordPress, `requests` gère souvent bien
    r.encoding = r.apparent_encoding or r.encoding
    return r.text or ""


# -----------------------------
# Récupération & Normalisation
# -----------------------------

@dataclass
class NormStore:
    name: str = ""
    address1: str = ""
    address2: str = ""
    city: str = ""
    region: str = ""
    postal_code: str = ""
    country: str = ""
    phone: str = ""
    website: str = ""
    lat: t.Optional[float] = None
    lng: t.Optional[float] = None

    def to_row(self) -> dict:
        return {
            "name": self.name,
            "address1": self.address1,
            "address2": self.address2,
            "city": self.city,
            "region": self.region,
            "postal_code": self.postal_code,
            "country": self.country,
            "phone": self.phone,
            "website": self.website,
            "lat": self.lat,
            "lng": self.lng,
        }


def normalize_item(item: dict) -> NormStore:
    """
    Adapter/renommer les champs reçus de l'API Stockist.
    """
    # Champs courants renvoyés par Stockist
    name = item.get("name") or item.get("store_name") or ""
    address1 = item.get("address1") or ""
    address2 = item.get("address2") or ""
    city = item.get("city") or ""
    region = item.get("region") or item.get("state") or ""
    postal_code = item.get("postal_code") or item.get("zip") or ""
    country = item.get("country") or ""
    phone = item.get("phone") or ""
    website = item.get("website") or item.get("url") or ""
    lat = item.get("lat") or item.get("latitude")
    lng = item.get("lng") or item.get("longitude")

    # Cast float si possible
    try:
        lat = float(lat) if lat is not None else None
    except Exception:
        lat = None
    try:
        lng = float(lng) if lng is not None else None
    except Exception:
        lng = None

    return NormStore(
        name=name,
        address1=address1,
        address2=address2,
        city=city,
        region=region,
        postal_code=postal_code,
        country=country,
        phone=phone,
        website=website,
        lat=lat,
        lng=lng,
    )


def fetch_all_locations(account: str, referer_url: str) -> t.List[dict]:
    """
    Récupère toutes les pages de l'API Stockist pour un compte donné.
    """
    rows: t.List[dict] = []
    page = 1
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": referer_url,
    }

    _log(f"[API] account={account} overview={guess_overview_script(account)}")

    while True:
        endpoint = build_api_endpoint(account, page)
        _log(f"[API] GET {endpoint}")
        resp = requests.get(endpoint, headers=headers, timeout=HTTP_TIMEOUT)
        # 200 avec `[]` quand plus de données
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break

        for itm in data:
            rows.append(normalize_item(itm).to_row())
        _log(f"[API] page={page} items={len(data)} total={len(rows)}")
        if len(data) < PER_PAGE:
            break
        page += 1

    return rows


# -----------------------------
# Fonction principale
# -----------------------------

def scrape_stockist(url: str) -> t.List[dict]:
    """
    Point d'entrée utilisé par ton app.
    """
    _log(f"[ENTRY] url={url}")

    # 1) Override par variable d'env (recommandé)
    if STOCKIST_ACCOUNT_ENV:
        _log(f"[OVERRIDE] STOCKIST_ACCOUNT={STOCKIST_ACCOUNT_ENV}")
        return fetch_all_locations(STOCKIST_ACCOUNT_ENV, url)

    # 2) Fallback projet
    if DEFAULT_ACCOUNT:
        _log(f"[FALLBACK] DEFAULT_ACCOUNT={DEFAULT_ACCOUNT}")
        return fetch_all_locations(DEFAULT_ACCOUNT, url)

    # 3) Détection dans le HTML (si tu veux laisser cette voie activée)
    html = fetch_html(url)
    acc = find_stockist_id_in_html(html)
    if acc:
        _log(f"[STATIC] store_id trouvé → {acc}")
        return fetch_all_locations(acc, url)

    # Rien trouvé → on échoue de manière explicite
    raise RuntimeError("Impossible de déterminer le store_id Stockist depuis la page.")


# -------------
# (Optionnel) – si tu veux tester localement :
if __name__ == "__main__":
    test_url = os.getenv("TEST_URL", "https://pieceandlove.fr/pages/distributeurs")
    out = scrape_stockist(test_url)
    print(f"Stores: {len(out)}")
    # Petit aperçu
    for r in out[:3]:
        print(r)
