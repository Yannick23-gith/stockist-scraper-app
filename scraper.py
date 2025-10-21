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

# Playwright (sync API)
from playwright.sync_api import sync_playwright, Response


# -----------------------------------------------------------------------------
# Logging (DEBUG si STOCKIST_DEBUG=1)
# -----------------------------------------------------------------------------
_LOG_LEVEL = logging.DEBUG if os.environ.get("STOCKIST_DEBUG") else logging.INFO
logging.basicConfig(
    level=_LOG_LEVEL,
    format="[stockist] %(levelname)s: %(message)s",
)
logger = logging.getLogger("stockist")


# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

COOKIE_ACCEPT_SELECTORS = [
    # boutons courants (à compléter si besoin)
    "button#onetrust-accept-btn-handler",
    "button[aria-label='Accepter']",
    "button:has-text('Accepter')",
    "button:has-text('Accept')",
    "button:has-text('OK')",
    "button:has-text('J’accepte')",
    "[data-testid='cookie-accept']",
]

STOCKIST_API_HINTS = (
    "/locations",  # la plupart sont /api/v1/locations ou /api/locations
    "stockist.co/api",
    "stockist.co/locations",
    "/api/v1/locations",
    "locations.json",
)


def _debug(msg: str) -> None:
    if _LOG_LEVEL == logging.DEBUG:
        logger.debug(msg)


# -----------------------------------------------------------------------------
# 1) Extraction statique d’un store_id quand il est visible dans le HTML
# -----------------------------------------------------------------------------
def _try_extract_store_id_static(url: str) -> Optional[str]:
    """
    Tente de trouver un store_id directement dans le HTML (rapide, sans navigateur).
    """
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=20)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        logger.warning(f"Échec GET pour lecture statique: {e}")
        return None

    # Quelques regex fréquentes
    patterns = [
        r'store_id["\']?\s*[:=]\s*["\']?(\d{4,})',             # store_id: 12345  ou "store_id": "12345"
        r'stockist_store_id["\']?\s*[:=]\s*["\']?(\d{4,})',    # stockist_store_id
        r'locations\.json\?[^"]*store_id=(\d{4,})',            # ...locations.json?store_id=12345
        r'stockist\.co\/api\/v1\/locations[^"]*store_id=(\d{4,})',  # URL complète dans un script
    ]
    for pat in patterns:
        m = re.search(pat, html, flags=re.IGNORECASE)
        if m:
            sid = m.group(1)
            _debug(f"[STATIC] store_id trouvé via '{pat}': {sid}")
            return sid

    _debug("[STATIC] Aucun store_id trouvé")
    return None


# -----------------------------------------------------------------------------
# 2) Playwright : clique cookies + capture du premier appel API Stockist
# -----------------------------------------------------------------------------
def _extract_store_id_with_playwright_or_capture_api(url: str) -> Dict[str, Any]:
    """
    Ouvre la page avec Playwright, accepte les cookies si nécessaire,
    écoute les réponses réseau pour trouver l'endpoint 'locations' de Stockist,
    et remonte :
      - 'store_id' (si déductible),
      - 'endpoint' (URL complète de l’API capturée),
      - 'first_chunk' (données JSON de la 1ère page si dispo).
    """
    out: Dict[str, Any] = {"store_id": None, "endpoint": None, "first_chunk": None}
    _debug("[PW] Démarrage Playwright")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()

        first_api_json: Optional[Dict[str, Any]] = None
        captured_endpoint: Optional[str] = None

        def on_response(res: Response) -> None:
            try:
                url_res = res.url
                # Ne garder que les endpoints Stockist (locations)
                if any(h in url_res for h in STOCKIST_API_HINTS):
                    if res.request.method.lower() in ("get", "post"):
                        if res.status == 200:
                            ctype = res.headers.get("content-type", "")
                            if "json" in ctype:
                                data = res.json()
                                nonlocal first_api_json, captured_endpoint
                                # On retient le premier JSON d'endpoint Stockist
                                if first_api_json is None:
                                    first_api_json = data
                                    captured_endpoint = url_res
                                    _debug(f"[PW] Endpoint capturé: {captured_endpoint}")
                                    # inutile de “stopper” - on collecte une seule fois
            except Exception:
                pass

        page.on("response", on_response)

        # Aller sur la page
        _debug(f"[PW] GOTO => {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # Cliquer sur la bannière cookies
        for sel in COOKIE_ACCEPT_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=1500)
                    _debug(f"[PW] Cookie banner validée via {sel}")
                    break
            except Exception:
                continue

        # Laisser la page déclencher les scripts (JS embed)
        page.wait_for_timeout(5000)  # <- important (augmenté)

        # Forcer un état “réseau calme”
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass  # pas grave

        # 2ème essai pour cookies tardifs
        for sel in COOKIE_ACCEPT_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=1500)
                    _debug(f"[PW] Cookie banner (2ème passe) via {sel}")
                    break
            except Exception:
                continue

        # Petite pause supplémentaire (donne sa chance aux XHR)
        page.wait_for_timeout(2000)

        # Récupérer le HTML (au cas où le store_id est dans un script inline)
        try:
            html = page.content()
        except Exception:
            html = ""

        # Fermer proprement
        context.close()
        browser.close()

    # Déduction du store_id depuis la 1ère réponse capturée
    captured_store_id = None
    if first_api_json:
        # plusieurs schémas existent chez Stockist
        #  - meta.store_id
        #  - request/store_id dans l’URL
        #  - une clé 'store_id' haut niveau
        captured_store_id = (
            first_api_json.get("store_id")
            or (first_api_json.get("meta", {}) or {}).get("store_id")
        )
        _debug(f"[PW] first_chunk présent, store_id(json)={captured_store_id}")

    # Sinon essayer de le déduire depuis l’URL capturée
    if not captured_store_id and captured_endpoint:
        try:
            q = parse_qs(urlparse(captured_endpoint).query)
            if "store_id" in q and q["store_id"]:
                captured_store_id = q["store_id"][0]
                _debug(f"[PW] store_id trouvé via endpoint: {captured_store_id}")
        except Exception:
            pass

    # Dernier recours : regex sur HTML
    if not captured_store_id and html:
        sid = _search_store_id_in_html(html)
        if sid:
            captured_store_id = sid
            _debug(f"[PW] store_id trouvé dans HTML: {sid}")

    out["store_id"] = captured_store_id
    out["endpoint"] = captured_endpoint
    out["first_chunk"] = first_api_json
    return out


def _search_store_id_in_html(html: str) -> Optional[str]:
    pats = [
        r'store_id["\']?\s*[:=]\s*["\']?(\d{4,})',
        r'stockist_store_id["\']?\s*[:=]\s*["\']?(\d{4,})',
        r'locations\.json\?[^"]*store_id=(\d{4,})',
    ]
    for pat in pats:
        m = re.search(pat, html, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None


# -----------------------------------------------------------------------------
# 3) Requêtes API : cas “on connaît l’endpoint exact” vs “on a juste le store_id”
# -----------------------------------------------------------------------------
def _fetch_all_locations_from_known_endpoint(endpoint: str, referer: str) -> List[Dict[str, Any]]:
    """
    On a l’URL exacte (celle interceptée). On rejoue les pages en incrémentant 'page=' si présent.
    """
    out: List[Dict[str, Any]] = []
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    s.headers["Referer"] = referer

    # parser l’URL pour manipuler les params
    parsed = urlparse(endpoint)
    q = parse_qs(parsed.query)
    page = int(q.get("page", ["1"])[0])

    while True:
        q["page"] = [str(page)]
        new_q = urlencode({k: v[0] if isinstance(v, list) and v else v for k, v in q.items()})
        new_url = urlunparse(parsed._replace(query=new_q))

        _debug(f"[API/endpoint] GET {new_url}")
        r = s.get(new_url, timeout=35)
        r.raise_for_status()
        data = r.json()

        items = _extract_locations_array(data)
        _debug(f"[API/endpoint] page={page} items={len(items)}")
        out.extend(items)

        # stop si moins que per_page (ou data vide)
        if not items or len(items) < _guess_per_page(q):
            break

        page += 1
        time.sleep(0.15)

    return out


def _fetch_all_locations_with_store_id(store_id: str, referer: str) -> List[Dict[str, Any]]:
    """
    On ne connaît que le store_id => on use l’endpoint commun Stockist.
    La forme la plus commune:
      https://stockist.co/api/v1/locations?store_id=XXXX&page=1&per_page=250
    """
    out: List[Dict[str, Any]] = []
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    s.headers["Referer"] = referer

    per_page = 250
    page = 1

    base = "https://stockist.co/api/v1/locations"
    # alternative si certains sites utilisent locations.json
    alt = "https://stockist.co/api/v1/locations.json"

    def get_url(p: int) -> str:
        return f"{base}?store_id={store_id}&page={p}&per_page={per_page}"

    while True:
        url = get_url(page)
        _debug(f"[API/store_id] GET {url}")

        r = s.get(url, timeout=35)
        if r.status_code == 404:
            # tenter la variante .json
            url = f"{alt}?store_id={store_id}&page={page}&per_page={per_page}"
            _debug(f"[API/store_id] Tentative alt => {url}")
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


def _extract_locations_array(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Les payloads varient:
      - {"locations":[...], "meta":{...}}
      - {"data":[...], "meta":{...}}
      - ou parfois directement une liste.
    """
    if isinstance(data, list):
        return data
    for k in ("locations", "data", "items", "results"):
        if k in data and isinstance(data[k], list):
            return data[k]
    # parfois { "stores": { "locations": [...] } }
    if "stores" in data and isinstance(data["stores"], dict):
        inner = data["stores"].get("locations")
        if isinstance(inner, list):
            return inner
    return []


def _guess_per_page(query: Dict[str, Any]) -> int:
    try:
        per = int(query.get("per_page", [250])[0])
        return per
    except Exception:
        return 250


# -----------------------------------------------------------------------------
# 4) Normalisation des champs
# -----------------------------------------------------------------------------
def _normalize_location(loc: Dict[str, Any]) -> Dict[str, Any]:
    # champs fréquents côté Stockist
    return {
        "name": loc.get("name") or loc.get("store_name") or "",
        "address": loc.get("address") or loc.get("street") or "",
        "city": loc.get("city") or "",
        "state": loc.get("state") or loc.get("province") or "",
        "postal_code": loc.get("postal_code") or loc.get("zip") or "",
        "country": loc.get("country") or "",
        "phone": loc.get("phone") or "",
        "website": loc.get("website") or loc.get("url") or "",
        "lat": loc.get("latitude") or loc.get("lat"),
        "lng": loc.get("longitude") or loc.get("lng"),
        "raw": loc,  # conserve le brut
    }


# -----------------------------------------------------------------------------
# 5) Entrée principale utilisée par app.py
# -----------------------------------------------------------------------------
def scrape_stockist(url: str) -> List[Dict[str, Any]]:
    """
    Stratégie :
      1) Tenter d’extraire store_id statiquement (ultra-rapide).
      2) Sinon : Playwright → cliquer cookies, capturer l’endpoint et/ou le store_id.
      3) Si un endpoint complet est capturé, rejouer toutes les pages à partir de lui.
      4) Sinon, appeler l’API officielle avec store_id.
      5) Fallback : si 0 résultat, refaire un tour Playwright pour revalider endpoint/store_id.
    """
    # 1) Essai statique
    store_id = _try_extract_store_id_static(url)

    # 2) Playwright (si pas de store_id)
    first_chunk = None
    captured_endpoint = None
    if not store_id:
        captured = _extract_store_id_with_playwright_or_capture_api(url)
        store_id = captured.get("store_id")
        first_chunk = captured.get("first_chunk")
        captured_endpoint = captured.get("endpoint")

    # 3) Si on a déjà un endpoint + un 1er JSON, on rejoue depuis l’endpoint
    if first_chunk and captured_endpoint:
        _debug("[MAIN] Endpoint capturé : rejouer via endpoint")
        locations = _fetch_all_locations_from_known_endpoint(captured_endpoint, referer=url)
        return [_normalize_location(l) for l in locations]

    rows: List[Dict[str, Any]] = []

    # 4) On a un store_id → API “classique”
    if store_id:
        _debug(f"[MAIN] store_id={store_id} → appel API")
        locations = _fetch_all_locations_with_store_id(store_id, referer=url)
        rows = [_normalize_location(l) for l in locations]

        # 4.b Fallback : si 0 ligne, on force la capture réseau pour valider le bon store_id
        if not rows:
            _debug("[MAIN] 0 ligne avec store_id → fallback Playwright")
            captured = _extract_store_id_with_playwright_or_capture_api(url)
            cap_chunk = captured.get("first_chunk")
            cap_endpoint = captured.get("endpoint")
            cap_sid = captured.get("store_id")

            if cap_chunk and cap_endpoint:
                _debug("[MAIN] Fallback endpoint → rejouer via endpoint capturé")
                locs2 = _fetch_all_locations_from_known_endpoint(cap_endpoint, referer=url)
                rows = [_normalize_location(l) for l in locs2]
            elif cap_sid and cap_sid != store_id:
                _debug(f"[MAIN] Nouveau store_id détecté: {cap_sid} (ancien={store_id})")
                locs2 = _fetch_all_locations_with_store_id(cap_sid, referer=url)
                rows = [_normalize_location(l) for l in locs2]

        if rows:
            return rows

    # 5) Toujours rien → erreur claire (l’UI affichera le message)
    raise RuntimeError("Impossible de déterminer le store_id Stockist depuis la page.")
