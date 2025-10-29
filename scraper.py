# scraper.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import json
import time
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from requests import Response as RequestsResponse

# Playwright (sync)
from playwright.sync_api import sync_playwright, Response as PWResponse

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
_LOG_LEVEL = logging.DEBUG if os.environ.get("STOCKIST_DEBUG") else logging.INFO
logging.basicConfig(level=_LOG_LEVEL, format="[stockist] %(levelname)s: %(message)s")
logger = logging.getLogger("stockist")

def _debug(msg: str) -> None:
    if _LOG_LEVEL == logging.DEBUG:
        logger.debug(msg)

# -----------------------------------------------------------------------------
# Constantes
# -----------------------------------------------------------------------------
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

STOCKIST_API_HINTS = (
    "/locations",
    "stockist.co/api",
    "stockist.co/locations",
    "/api/v1/locations",
    "locations.json",
)

COOKIE_ACCEPT_SELECTORS = [
    "button#onetrust-accept-btn-handler",
    "button[aria-label='Accepter']",
    "button:has-text('Accepter')",
    "button:has-text('Accept')",
    "button:has-text('OK')",
    "button:has-text('J’accepte')",
    "[data-testid='cookie-accept']",
    "button:has-text('I agree')",
]

# -----------------------------------------------------------------------------
# Outils HTML "agressifs" pour trouver store_id sans navigateur
# -----------------------------------------------------------------------------
def _http_get(url: str, timeout: int = 25) -> Optional[str]:
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"[STATIC] GET {url} échoué: {e}")
        return None

def _extract_all_script_src(html: str) -> List[str]:
    # très permissif
    return re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, flags=re.I)

def _extract_all_iframe_src(html: str) -> List[str]:
    return re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, flags=re.I)

def _extract_all_link_href(html: str) -> List[str]:
    return re.findall(r'<link[^>]+href=["\']([^"\']+)["\']', html, flags=re.I)

def _extract_all_anchor_href(html: str) -> List[str]:
    return re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, flags=re.I)

def _search_store_id_anywhere(text: str) -> Optional[str]:
    patterns = [
        r'store_id["\']?\s*[:=]\s*["\']?(\d{4,})',
        r'stockist_store_id["\']?\s*[:=]\s*["\']?(\d{4,})',
        r'locations\.json\?[^"\']*store_id=(\d{4,})',
        r'api\/v1\/locations[^"\']*store_id=(\d{4,})',
        r'\bstoreId["\']?\s*[:=]\s*["\']?(\d{4,})',
        r'data-store-id=["\'](\d{4,})',
        r'data-stockist-store-id=["\'](\d{4,})',
        r'stockist\.co\/[^"\']*store_id=(\d{4,})',
        r'\?store_id=(\d{4,})',
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(1)
    return None

def _try_extract_store_id_static(url: str) -> Optional[str]:
    """Essaye en statique: page HTML, tous les script src, iframes, links, ancres…"""
    html = _http_get(url)
    if not html:
        return None

    # 1) direct dans le HTML
    sid = _search_store_id_anywhere(html)
    if sid:
        _debug(f"[STATIC] store_id trouvé dans HTML: {sid}")
        return sid

    # 2) scanner tous les <script src=...>
    for src in _extract_all_script_src(html):
        full = src if re.match(r'^https?://', src, flags=re.I) else _absolutize(url, src)
        _debug(f"[STATIC] scan script: {full}")
        js = _http_get(full, timeout=20)
        if not js:
            continue
        sid = _search_store_id_anywhere(js)
        if sid:
            _debug(f"[STATIC] store_id trouvé dans script {full}: {sid}")
            return sid

    # 3) iframes
    for src in _extract_all_iframe_src(html):
        full = src if re.match(r'^https?://', src, flags=re.I) else _absolutize(url, src)
        _debug(f"[STATIC] scan iframe: {full}")
        inner = _http_get(full, timeout=20)
        if not inner:
            continue
        sid = _search_store_id_anywhere(inner)
        if sid:
            _debug(f"[STATIC] store_id trouvé dans iframe {full}: {sid}")
            return sid

    # 4) liens (rare mais déjà vu)
    for href in _extract_all_link_href(html) + _extract_all_anchor_href(html):
        if "store_id=" in href or "locations" in href:
            sid = _search_store_id_anywhere(href)
            if sid:
                _debug(f"[STATIC] store_id trouvé via href {href}: {sid}")
                return sid

    _debug("[STATIC] Aucun store_id trouvé")
    return None

def _absolutize(base_url: str, candidate: str) -> str:
    if candidate.startswith("//"):
        scheme = urlparse(base_url).scheme or "https"
        return f"{scheme}:{candidate}"
    if candidate.startswith("/"):
        up = urlparse(base_url)
        return f"{up.scheme}://{up.netloc}{candidate}"
    return candidate

# -----------------------------------------------------------------------------
# Playwright : capture endpoint + store_id
# -----------------------------------------------------------------------------
def _extract_with_playwright(url: str) -> Dict[str, Any]:
    out = {"store_id": None, "endpoint": None, "first_chunk": None}
    _debug("[PW] Launch")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()

        first_api_json: Optional[Dict[str, Any]] = None
        captured_endpoint: Optional[str] = None

        def on_response(res: PWResponse) -> None:
            try:
                u = res.url
                if any(h in u for h in STOCKIST_API_HINTS):
                    if res.status == 200 and res.request.method.lower() in ("get", "post"):
                        ctype = res.headers.get("content-type", "")
                        if "json" in ctype:
                            data = res.json()
                            nonlocal first_api_json, captured_endpoint
                            if first_api_json is None:
                                first_api_json = data
                                captured_endpoint = u
                                _debug(f"[PW] endpoint capturé: {captured_endpoint}")
            except Exception:
                pass

        page.on("response", on_response)

        _debug(f"[PW] GOTO {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # cookies
        for sel in COOKIE_ACCEPT_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=1500)
                    _debug(f"[PW] cookies via {sel}")
                    break
            except Exception:
                continue

        # attendre les XHR
        page.wait_for_timeout(5000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        # 2nd passe cookies
        for sel in COOKIE_ACCEPT_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=1500)
                    _debug(f"[PW] cookies (2) via {sel}")
                    break
            except Exception:
                continue

        page.wait_for_timeout(2000)

        try:
            html = page.content()
        except Exception:
            html = ""

        context.close()
        browser.close()

    # déduire store_id depuis json ou endpoint ou html
    captured_store_id = None
    if first_api_json:
        captured_store_id = (
            first_api_json.get("store_id")
            or (first_api_json.get("meta") or {}).get("store_id")
        )
        _debug(f"[PW] store_id(json)={captured_store_id}")
    if not captured_store_id and captured_endpoint:
        try:
            q = parse_qs(urlparse(captured_endpoint).query)
            if q.get("store_id"):
                captured_store_id = q["store_id"][0]
                _debug(f"[PW] store_id(endpoint)={captured_store_id}")
        except Exception:
            pass
    if not captured_store_id and html:
        sid = _search_store_id_anywhere(html)
        if sid:
            captured_store_id = sid
            _debug(f"[PW] store_id(HTML)={sid}")

    out["store_id"] = captured_store_id
    out["endpoint"] = captured_endpoint
    out["first_chunk"] = first_api_json
    return out

# -----------------------------------------------------------------------------
# Rejouer l’API
# -----------------------------------------------------------------------------
def _extract_locations_array(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("locations", "data", "items", "results"):
            v = data.get(k)
            if isinstance(v, list):
                return v
        if isinstance(data.get("stores"), dict):
            v = data["stores"].get("locations")
            if isinstance(v, list):
                return v
    return []

def _guess_per_page_from_query(q: Dict[str, List[str]]) -> int:
    try:
        return int(q.get("per_page", ["250"])[0])
    except Exception:
        return 250

def _fetch_from_endpoint(endpoint: str, referer: str) -> List[Dict[str, Any]]:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    s.headers["Referer"] = referer

    parsed = urlparse(endpoint)
    q = parse_qs(parsed.query)
    if "page" not in q:
        q["page"] = ["1"]
    per_page = _guess_per_page_from_query(q)
    page = int(q["page"][0])

    out: List[Dict[str, Any]] = []
    while True:
        q["page"] = [str(page)]
        new_q = urlencode({k: v[0] if isinstance(v, list) else v for k, v in q.items()})
        url = urlunparse(parsed._replace(query=new_q))
        _debug(f"[API/endpoint] GET {url}")

        r = s.get(url, timeout=35)
        r.raise_for_status()
        data = r.json()
        items = _extract_locations_array(data)
        _debug(f"[API/endpoint] page={page} items={len(items)}")
        out.extend(items)

        if not items or len(items) < per_page:
            break
        page += 1
        time.sleep(0.15)
    return out

def _fetch_from_store_id(store_id: str, referer: str) -> List[Dict[str, Any]]:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    s.headers["Referer"] = referer

    base = "https://stockist.co/api/v1/locations"
    alt = "https://stockist.co/api/v1/locations.json"
    per_page = 250
    page = 1

    out: List[Dict[str, Any]] = []
    while True:
        url = f"{base}?store_id={store_id}&page={page}&per_page={per_page}"
        _debug(f"[API/store_id] GET {url}")

        r = s.get(url, timeout=35)
        if r.status_code == 404:
            url = f"{alt}?store_id={store_id}&page={page}&per_page={per_page}"
            _debug(f"[API/store_id] alt => {url}")
            r = s.get(url, timeout=35)

        r.raise_for_status()
        data = r.json()
        items = _extract_locations_array(data)
        _debug(f"[API/store_id] page={page} items={len(items)}")
        out.extend(items)

        if not items or len(items) < per_page:
            break
        page += 1
        time.sleep(0.15)
    return out

# -----------------------------------------------------------------------------
# Normalisation
# -----------------------------------------------------------------------------
def _normalize_location(loc: Dict[str, Any]) -> Dict[str, Any]:
    def g(*keys, default=""):
        for k in keys:
            v = loc.get(k)
            if v not in (None, ""):
                return v
        return default

    name = g("name", "title", "store_name")
    address1 = g("address1", "address_1", "street", "line1", "address")
    address2 = g("address2", "address_2", "line2")
    city = g("city", "locality", "town")
    state = g("state", "region", "province")
    postal = g("postal_code", "postcode", "zip")
    country = g("country", "country_name", "country_code")
    phone = g("phone", "telephone", "tel")
    website = g("website", "url", "link")
    lat = g("lat", "latitude")
    lng = g("lng", "lon", "longitude")

    # Adresse complète pour la lisibilité et la dédup
    parts = [address1, address2, city, state, postal, country]
    address_full = ", ".join([p for p in parts if p])

    return {
        "name": name,
        "address1": address1,
        "address2": address2,
        "city": city,
        "state": state,
        "postal_code": postal,
        "country": country,
        "phone": phone,
        "website": website,
        "lat": lat,
        "lng": lng,
        "address_full": address_full,
    }

# -----------------------------------------------------------------------------
# Entrée principale
# -----------------------------------------------------------------------------
def scrape_stockist(url: str) -> List[Dict[str, Any]]:
    # 1) extraction statique agressive
    store_id = _try_extract_store_id_static(url)

    # 2) sinon Playwright
    first_chunk = None
    captured_endpoint = None
    if not store_id:
        cap = _extract_with_playwright(url)
        store_id = cap.get("store_id")
        first_chunk = cap.get("first_chunk")
        captured_endpoint = cap.get("endpoint")

    # 3) endpoint capturé → rejouer via endpoint (fiable)
    if first_chunk and captured_endpoint:
        _debug("[MAIN] endpoint capturé → replay endpoint")
        locs = _fetch_from_endpoint(captured_endpoint, referer=url)
        return [_normalize(x) for x in locs]

    rows: List[Dict[str, Any]] = []

    # 4) store_id connu → API Stockist
    if store_id:
        _debug(f"[MAIN] store_id={store_id} → API stockist")
        locs = _fetch_from_store_id(store_id, referer=url)
        rows = [_normalize(x) for x in locs]

        # fallback si 0
        if not rows:
            _debug("[MAIN] 0 ligne → fallback Playwright pour recapture")
            cap = _extract_with_playwright(url)
            cap_chunk = cap.get("first_chunk")
            cap_ep = cap.get("endpoint")
            cap_sid = cap.get("store_id")

            if cap_chunk and cap_ep:
                _debug("[MAIN] fallback endpoint → replay endpoint capturé")
                locs2 = _fetch_from_endpoint(cap_ep, referer=url)
                rows = [_normalize(x) for x in locs2]
            elif cap_sid and cap_sid != store_id:
                _debug(f"[MAIN] nouveau store_id détecté: {cap_sid} (ancien={store_id})")
                locs2 = _fetch_from_store_id(cap_sid, referer=url)
                rows = [_normalize(x) for x in locs2]

        if rows:
            return rows

    # 5) plus rien
    raise RuntimeError("Impossible de déterminer le store_id Stockist depuis la page.")
