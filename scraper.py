import os
import re
import csv
import json
import typing as t
from dataclasses import dataclass

import requests

STOCKIST_DEBUG = os.getenv("STOCKIST_DEBUG", "0") not in ("0", "", "false", "False", "FALSE")
STOCKIST_ACCOUNT_ENV = os.getenv("STOCKIST_ACCOUNT", "").strip()
DEFAULT_ACCOUNT = "u20439"  # tu peux l’enlever si tu veux forcer l’ENV

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 30
PER_PAGE = 200


def _log(msg: str) -> None:
    if STOCKIST_DEBUG:
        print(f"[stockist] DEBUG: {msg}", flush=True)


# ---------- Normalisation ----------

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


# ---------- Endpoints helpers ----------

def api_base(account: str) -> str:
    return f"https://stockist.co/api/v1/{account}"


def build_candidates(account: str, page: int) -> t.List[str]:
    base = api_base(account)
    # ordre de tentative
    candidates = [
        f"{base}/locations.json?page={page}&per_page={PER_PAGE}",
        f"{base}/locations?page={page}&per_page={PER_PAGE}",
        f"{base}/locations/overview.json?page={page}&per_page={PER_PAGE}",
        # overview.js n’est pas paginé en général, mais on garde ?page pour les logs
        f"{base}/locations/overview.js?page={page}&per_page={PER_PAGE}",
    ]
    return candidates


def parse_overview_js(text: str) -> t.List[dict]:
    """
    Beaucoup de comptes exposent leurs points de vente via un JS de type:
        window.Stockist.locations = [...]
    ou
        Stockist.locations = [...]
    On extrait le premier tableau JSON trouvé dans le script.
    """
    # Cherche un tableau JSON le plus large possible
    # Exemples de patterns vus: "= [ ... ];", "locations:[...]", etc.
    m = re.search(r"=\s*(\[\s*\{.*?\}\s*\])\s*;?", text, re.S)
    if not m:
        # fallback : array après "locations:"
        m = re.search(r"locations\s*:\s*(\[\s*\{.*?\}\s*\])", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
        if isinstance(arr, list):
            return arr
    except Exception:
        return []
    return []


def try_fetch_endpoint(url: str, headers: dict) -> t.Tuple[bool, t.Union[list, None]]:
    _log(f"[API] TRY {url}")
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    if r.status_code == 404:
        _log("[API] 404 → next pattern")
        return False, None
    r.raise_for_status()

    ct = r.headers.get("Content-Type", "")
    txt = r.text or ""

    # JSON direct ?
    if "application/json" in ct or txt.strip().startswith("["):
        try:
            data = r.json()
            if isinstance(data, list):
                return True, data
        except Exception:
            pass

    # JS overview ?
    if url.endswith(".js"):
        data = parse_overview_js(txt)
        if data:
            return True, data

    # Certains serveurs marquent overview.js "text/javascript" mais c'est OK
    if "javascript" in ct and not url.endswith(".json"):
        data = parse_overview_js(txt)
        if data:
            return True, data

    # Autres cas → on tente quand même JSON
    try:
        data = r.json()
        if isinstance(data, list):
            return True, data
    except Exception:
        pass

    # Rien de reconnu sur cet endpoint
    _log("[API] unrecognized payload on this endpoint")
    return False, None


def fetch_all_locations(account: str, referer_url: str) -> t.List[dict]:
    rows: t.List[dict] = []
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": referer_url,
    }

    page = 1
    total = 0
    while True:
        got = False
        for endpoint in build_candidates(account, page):
            ok, payload = try_fetch_endpoint(endpoint, headers)
            if not ok:
                continue

            got = True
            if not payload:
                _log(f"[API] page={page} items=0 total={total}")
                break

            # on normalise
            for itm in payload:
                rows.append(normalize_item(itm).to_row())

            total = len(rows)
            _log(f"[API] page={page} items={len(payload)} total={total}")

            # overview.js renvoie souvent tout en une fois → on stoppe à la 1re page
            if endpoint.endswith(".js"):
                return rows

            # pagination API JSON : si < PER_PAGE on stoppe
            if len(payload) < PER_PAGE:
                return rows

            break  # ne pas essayer les autres patterns pour cette page

        if not got:
            # Aucun des endpoints n'a retourné quelque chose d'utilisable pour cette page
            if page == 1 and not rows:
                raise requests.HTTPError(f"All endpoints 404/unsupported for account {account}.")
            # Si on a déjà eu des données avant, on s'arrête proprement
            return rows

        page += 1


# ---------- Détection de l’ID dans le HTML si besoin ----------

_PATTERNS = [
    re.compile(r"https?://stockist\.co/api/v1/(u\d+)/locations", re.I),
    re.compile(r'"account_id"\s*:\s*"(u\d+)"', re.I),
    re.compile(r"data-account\s*=\s*\"(u\d+)\"", re.I),
    re.compile(r"data-stockist-account\s*=\s*\"(u\d+)\"", re.I),
]


def fetch_html(url: str) -> str:
    _log(f"[STATIC] GET {url}")
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text or ""


def find_stockist_id_in_html(html: str) -> t.Optional[str]:
    if not html:
        return None
    for pat in _PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None


# ---------- Entrée principale ----------

def scrape_stockist(url: str) -> t.List[dict]:
    _log(f"[ENTRY] url={url}")

    if STOCKIST_ACCOUNT_ENV:
        _log(f"[OVERRIDE] STOCKIST_ACCOUNT={STOCKIST_ACCOUNT_ENV}")
        return fetch_all_locations(STOCKIST_ACCOUNT_ENV, url)

    if DEFAULT_ACCOUNT:
        _log(f"[FALLBACK] DEFAULT_ACCOUNT={DEFAULT_ACCOUNT}")
        return fetch_all_locations(DEFAULT_ACCOUNT, url)

    html = fetch_html(url)
    acc = find_stockist_id_in_html(html)
    if acc:
        _log(f"[STATIC] store_id trouvé → {acc}")
        return fetch_all_locations(acc, url)

    raise RuntimeError("Impossible de déterminer le store_id Stockist depuis la page.")


if __name__ == "__main__":
    test_url = os.getenv("TEST_URL", "https://pieceandlove.fr/pages/distributeurs")
    out = scrape_stockist(test_url)
    print(f"Stores: {len(out)}")
    for r in out[:3]:
        print(r)
