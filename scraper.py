# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright
import re, time, json
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

NAV_TIMEOUT = 120_000         # navigation prudente
CAPTURE_WINDOW_MS = 30_000    # écoute initiale pour récupérer 1ère URL JSON
MAX_PAGES = 200               # plafond de sécurité

def _norm(s): 
    return (s or "").strip()

def _mk_row(loc: dict):
    name   = loc.get("name") or loc.get("title") or loc.get("store_name") or ""
    addr1  = loc.get("address1") or loc.get("street") or loc.get("address_line_1") or ""
    addr2  = loc.get("address2") or loc.get("address_line_2") or ""
    city   = loc.get("city") or ""
    state  = loc.get("state") or loc.get("province") or ""
    postal = loc.get("postal_code") or loc.get("postcode") or loc.get("zip") or ""
    country= loc.get("country") or loc.get("country_code") or ""
    website= loc.get("website") or loc.get("url") or ""

    address_full = ", ".join([_norm(x) for x in [addr1, addr2, postal and f"{postal} {city}" or city, state, country] if _norm(x)])

    return {
        "name": _norm(name),
        "address_full": address_full,
        "street": _norm(addr1),
        "city": _norm(city),
        "postal_code": _norm(postal),
        "country": _norm(country),
        "url": _norm(website),
    }

def _extract_locations(data):
    out = []
    if isinstance(data, dict):
        for k in ("locations","results","stores","items","data","payload"):
            if k in data:
                v = data[k]
                if isinstance(v, list):
                    out.extend(v)
                elif isinstance(v, dict):
                    for kk in ("locations","results","stores","items"):
                        if kk in v and isinstance(v[kk], list):
                            out.extend(v[kk])
    elif isinstance(data, list):
        out.extend(data)
    return out

def _set_query_param(u, key, value):
    """Retourne l'URL u en remplaçant/ajoutant le paramètre key=value."""
    parsed = urlparse(u)
    q = parse_qs(parsed.query)
    q[key] = [str(value)]
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse(parsed._replace(query=new_q))

def scrape_stockist(url: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--single-process","--no-zygote"]
        )
        context = browser.new_context(
            locale="fr-FR",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/123 Safari/537.36")
        )
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)
        page.set_default_timeout(NAV_TIMEOUT)

        first_json_url = {"url": None}

        def handle_response(resp):
            try:
                u = (resp.url or "").lower()
                ct = resp.headers.get("content-type","").lower()
                if first_json_url["url"] is None and (("stocki.st" in u) or ("stockist" in u)) and ("json" in ct or u.endswith(".json")):
                    first_json_url["url"] = resp.url  # garder l’URL EXACTE
                    # log utile
                    print(f"[SCRAPER] first JSON URL: {first_json_url['url']}")
            except Exception:
                pass

        # capter aussi les frames
        context.on("response", handle_response)

        # 1) ouvrir la page (pas 'networkidle')
        page.goto(url, wait_until="domcontentloaded")

        # 2) laisser le widget faire au moins 1 requête JSON
        t0 = time.time()
        while (time.time() - t0) * 1000 < CAPTURE_WINDOW_MS and not first_json_url["url"]:
            page.wait_for_timeout(250)

        # 3) si rien capté, tenter de stimuler une iframe stockist
        if not first_json_url["url"]:
            try:
                for f in page.frames:
                    uu = (f.url or "").lower()
                    if "stocki.st" in uu or "stockist" in uu or "stockist.co" in uu:
                        for _ in range(10):
                            try:
                                f.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                                page.wait_for_timeout(150)
                            except Exception:
                                break
                        # petite fenêtre d’écoute supplémentaire
                        t1 = time.time()
                        while (time.time() - t1) * 1000 < 5_000 and not first_json_url["url"]:
                            page.wait_for_timeout(250)
                        break
            except Exception:
                pass

        # 4) Paginer côté API à partir de la première URL trouvée
        rows = []
        if first_json_url["url"]:
            api = context.request  # même contexte (cookies, referer…)
            page_num = 1
            seen_ids = set()

            # Essayer de détecter le paramètre 'page' et 'per_page'
            base_url = first_json_url["url"]
            # s'il n’y a pas page, on l’ajoute
            if "page=" not in base_url:
                base_url = _set_query_param(base_url, "page", page_num)
            # assurer un per_page correct si présent (100 max souvent)
            if "per_page=" in base_url:
                base_url = _set_query_param(base_url, "per_page", 100)

            while page_num <= MAX_PAGES:
                page_url = _set_query_param(base_url, "page", page_num)
                r = api.get(page_url, timeout=60_000)
                if r.ok:
                    try:
                        data = r.json()
                    except Exception:
                        break
                    locs = _extract_locations(data)
                    print(f"[SCRAPER] page {page_num}: {len(locs)} items")
                    if not locs:
                        break
                    for loc in locs:
                        # essaye d'utiliser un id si fourni
                        loc_id = loc.get("id") or loc.get("location_id") or json.dumps(loc, sort_keys=True)[:80]
                        if loc_id in seen_ids:
                            continue
                        seen_ids.add(loc_id)
                        rows.append(_mk_row(loc))
                    page_num += 1
                    # petite pause anti-rate limit
                    time.sleep(0.15)
                else:
                    # fin/pas d'autres pages ou rate limit
                    break
        else:
            print("[SCRAPER] Aucune URL JSON capturée (widget non détecté ou bloqué).")

        browser.close()

    # dédup sécurité par (nom, adresse_full)
    dedup, out = set(), []
    for r in rows:
        k = (r["name"].lower(), r["address_full"].lower())
        if k in dedup:
            continue
        dedup.add(k)
        out.append(r)

    print(f"[SCRAPER] TOTAL: {len(out)} magasins (après pagination & dédup)")
    return out
