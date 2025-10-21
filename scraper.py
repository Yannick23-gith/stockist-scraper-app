# scraper.py
# -*- coding: utf-8 -*-

import json
import re
import time
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

from playwright.sync_api import sync_playwright

# --- Réglages généraux --------------------------------------------------------

NAV_TIMEOUT = 45_000          # Navigation Playwright (ms)
CAPTURE_WINDOW_MS = 8_000     # Fenêtre d'écoute réseaux (fallback)
MAX_PAGES = 100               # Sécurité pagination API (100x100 = 10 000)


# --- Petites utilitaires ------------------------------------------------------

def _set_query_param(url: str, key: str, value) -> str:
    """
    Ajoute/remplace un paramètre de requête dans une URL (sans casser le reste).
    """
    urlp = urlparse(url)
    q = dict(parse_qsl(urlp.query, keep_blank_values=True))
    q[str(key)] = str(value)
    new_qs = urlencode(q, doseq=True)
    return urlunparse((urlp.scheme, urlp.netloc, urlp.path, urlp.params, new_qs, urlp.fragment))


def _extract_locations(data):
    """
    Tente d'extraire un tableau de 'locations' d'une réponse JSON,
    en couvrant plusieurs structures courantes des APIs Stockist.
    """
    if not data:
        return []

    # 1) Réponse déjà un tableau
    if isinstance(data, list):
        return data

    # 2) Clés standard
    for key in ("locations", "results", "items", "data", "records"):
        if isinstance(data, dict) and key in data and isinstance(data[key], list):
            return data[key]

    # 3) GraphQL-like
    if isinstance(data, dict):
        # "data": {"locations": [...]}
        d = data.get("data")
        if isinstance(d, dict):
            for key in ("locations", "results", "items", "records"):
                if key in d and isinstance(d[key], list):
                    return d[key]

        # pagination: {"meta": {"total_pages": X}, "data": [...]} => data déjà couvert ci-dessus
        # rien d'autre? on retourne vide.
    return []


def _mk_row(loc: dict) -> dict:
    """
    Normalise un objet 'location' en un dict plat (idéal pour CSV).
    On récolte l'essentiel (nom, adresse, ville, pays, etc.).
    """
    name = (
        loc.get("name")
        or loc.get("title")
        or loc.get("store_name")
        or loc.get("company")
        or ""
    )

    # Adresse
    addr = loc.get("address") or {}
    if isinstance(addr, dict):
        line1 = addr.get("line1") or addr.get("address1") or addr.get("street") or ""
        line2 = addr.get("line2") or addr.get("address2") or ""
        city = addr.get("city") or ""
        state = addr.get("state") or addr.get("province") or ""
        postal = addr.get("postal_code") or addr.get("zip") or ""
        country = addr.get("country") or ""
    else:
        # Parfois c'est tout plat
        line1 = loc.get("address1") or loc.get("street") or ""
        line2 = loc.get("address2") or ""
        city = loc.get("city") or ""
        state = loc.get("state") or loc.get("province") or ""
        postal = loc.get("postal_code") or loc.get("zip") or ""
        country = loc.get("country") or ""

    # Téléphone/site
    phone = loc.get("phone") or loc.get("telephone") or ""
    website = loc.get("website") or loc.get("url") or ""

    # Géoloc
    lat = loc.get("lat") or loc.get("latitude") or ""
    lng = loc.get("lng") or loc.get("lon") or loc.get("longitude") or ""

    # Texte adresse complète
    address_full = ", ".join([x for x in [line1, line2, city, state, postal, country] if x])

    return {
        "name": name.strip(),
        "address1": line1.strip(),
        "address2": line2.strip(),
        "city": city.strip(),
        "state": state.strip(),
        "postal_code": postal.strip(),
        "country": country.strip(),
        "phone": str(phone).strip(),
        "website": website.strip(),
        "lat": lat,
        "lng": lng,
        "address_full": address_full.strip(),
    }


# --- Accès API direct via store_id --------------------------------------------

def _scrape_via_api_store_id(store_id: str):
    """
    Paginer l’API officielle Stockist si on connaît le store_id (fiable et rapide).
    Essaie plusieurs domaines (varie selon les intégrations).
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process", "--no-zygote"],
        )
        context = browser.new_context(locale="fr-FR")

        endpoints = [
            f"https://app.stockist.co/api/stores/{store_id}/locations",
            f"https://stocki.st/api/stores/{store_id}/locations",
            f"https://stockist.co/api/stores/{store_id}/locations",
        ]

        rows, seen = [], set()
        for base in endpoints:
            try:
                page_num = 1
                while page_num <= MAX_PAGES:
                    page_url = _set_query_param(_set_query_param(base, "per_page", 100), "page", page_num)
                    r = context.request.get(page_url, timeout=60_000)
                    print(f"[SCRAPER] GET {page_url} -> HTTP {r.status}")
                    if not r.ok:
                        break

                    data = None
                    try:
                        data = r.json()
                    except Exception:
                        txt = r.text()
                        # JSONP -> extraire les {} ou []
                        m = re.search(r"\(\s*({[\s\S]*}|[\[\s\S]*?)\)\s*;?\s*$", txt)
                        if m:
                            data = json.loads(m.group(1))

                    locs = _extract_locations(data) if data else []
                    if not locs:
                        break

                    for loc in locs:
                        lid = loc.get("id") or loc.get("location_id") or json.dumps(loc, sort_keys=True)[:80]
                        if lid in seen:
                            continue
                        seen.add(lid)
                        rows.append(_mk_row(loc))

                    page_num += 1
                    time.sleep(0.08)
            except Exception as e:
                print(f"[SCRAPER] endpoint failed {base}: {e}")
                continue

        browser.close()

    # Dédup légère
    out, s = [], set()
    for r in rows:
        k = (r["name"].lower(), r["address_full"].lower())
        if k in s:
            continue
        s.add(k)
        out.append(r)
    print(f"[SCRAPER] TOTAL: {len(out)} magasins (store_id direct)")
    return out


# --- Scraper principal ---------------------------------------------------------

def scrape_stockist(url: str):
    """
    Stratégie robuste :
      1) Si 'url' est *directement* un store_id (ex: "12345"), on attaque l’API.
      2) Sinon on ouvre la page, on tente d’extraire l’ID de boutique (store_id) depuis le HTML.
         - si on l’a : on pagine l’API (rapide et stable)
         - sinon : fallback interception réseau + pagination générique
    """
    url = (url or "").strip()

    # 1) Saisie directe d'un store_id (ultra-pratique)
    if url.isdigit():
        return _scrape_via_api_store_id(url)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process", "--no-zygote"],
        )
        context = browser.new_context(
            locale="fr-FR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)
        page.set_default_timeout(NAV_TIMEOUT)

        print(f"[SCRAPER] goto {url}")
        page.goto(url, wait_until="domcontentloaded")

        # --- A) Essayer d'identifier store_id depuis la page ------------------
        store_id = None
        try:
            # scripts <script src="...stockist... ?store=12345">
            scripts = page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
            for s in scripts:
                sl = (s or "").lower()
                if "stockist" in sl and "store=" in sl:
                    m = re.search(r"[?&]store=(\d+)", s)
                    if m:
                        store_id = m.group(1)
                        print(f"[SCRAPER] store_id via script src: {store_id}")
                        break

            # tags/JSON embarqué
            if not store_id:
                html = page.content()
                m = re.search(r'data-stockist-store=["\'](\d+)["\']', html, flags=re.I)
                if not m:
                    m = re.search(r'"store_id"\s*:\s*(\d+)', html, flags=re.I)
                if not m:
                    m = re.search(
                        r'stockist[^=]*=\s*{[^}]*store[_\s]*id["\']?\s*[:=]\s*("?)(\d+)\1',
                        html,
                        flags=re.I,
                    )
                if m:
                    store_id = m.group(1) if m.lastindex == 1 else (
                        m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(0)
                    )
                    print(f"[SCRAPER] store_id via HTML: {store_id}")
        except Exception as e:
            print(f"[SCRAPER] store_id parse error: {e}")

        # --- B) Si on a un store_id -> API directe ----------------------------
        if store_id and str(store_id).isdigit():
            rows = _scrape_via_api_store_id(str(store_id))
            browser.close()
            return rows

        # --- C) Fallback : interception réseau + pagination -------------------
        print("[SCRAPER] Fallback: interception réseau/pagination générique…")
        first_json_url = {"url": None}

        def handle_response(resp):
            try:
                u = (resp.url or "")
                lu = u.lower()
                if ("stockist" in lu or "stocki.st" in lu) and any(
                    k in lu for k in ("location", "locations", "store", "graphql", "api", "search")
                ):
                    ct = resp.headers.get("content-type", "")
                    print(f"[SCRAPER][CANDIDATE] {resp.status} {u} CT={ct}")
                    if first_json_url["url"] is None:
                        first_json_url["url"] = u
                        print(f"[SCRAPER] first JSON URL (candidate): {u}")
            except Exception:
                pass

        context.on("response", handle_response)

        # Laisser le temps au widget de pousser une requête
        t0 = time.time()
        while (time.time() - t0) * 1000 < CAPTURE_WINDOW_MS and not first_json_url["url"]:
            page.wait_for_timeout(250)

        # S'il est dans une iframe : scroller un peu pour le déclencher
        if not first_json_url["url"]:
            try:
                for f in page.frames:
                    uu = (f.url or "").lower()
                    if "stockist" in uu or "stocki.st" in uu:
                        for _ in range(10):
                            try:
                                f.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                                page.wait_for_timeout(180)
                            except Exception:
                                break
                        t1 = time.time()
                        while (time.time() - t1) * 1000 < 5000 and not first_json_url["url"]:
                            page.wait_for_timeout(250)
                        break
            except Exception:
                pass

        rows = []
        if first_json_url["url"]:
            api = context.request
            base = first_json_url["url"]

            # Normaliser la pagination
            if "page=" not in base:
                base = _set_query_param(base, "page", 1)
            base = _set_query_param(base, "per_page", 100)

            page_num = 1
            seen_ids = set()
            while page_num <= MAX_PAGES:
                page_url = _set_query_param(base, "page", page_num)
                r = api.get(page_url, timeout=60_000, headers={"Referer": url})
                print(f"[SCRAPER] GET {page_url} -> HTTP {r.status}")
                if not r.ok:
                    break

                data = None
                try:
                    data = r.json()
                except Exception:
                    txt = r.text()
                    m = re.search(r"\(\s*({[\s\S]*}|[\[\s\S]*?)\)\s*;?\s*$", txt)
                    if m:
                        data = json.loads(m.group(1))

                locs = _extract_locations(data) if data else []
                print(f"[SCRAPER] page {page_num}: {len(locs)} items")
                if not locs:
                    break

                for loc in locs:
                    lid = loc.get("id") or loc.get("location_id") or json.dumps(loc, sort_keys=True)[:80]
                    if lid in seen_ids:
                        continue
                    seen_ids.add(lid)
                    rows.append(_mk_row(loc))

                page_num += 1
                time.sleep(0.1)
        else:
            print("[SCRAPER] Aucune URL JSON capturée (widget non détecté).")

        browser.close()

    # Dédup finale
    out, seen = [], set()
    for r in rows:
        k = (r["name"].lower(), r["address_full"].lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(r)

    print(f"[SCRAPER] TOTAL: {len(out)} magasins (fallback)")
    return out
