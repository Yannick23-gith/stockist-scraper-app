# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright
import re, time, json

NAV_TIMEOUT = 120_000
CAPTURE_WINDOW_MS = 20_000   # temps pour intercepter la réponse JSON

def _norm(s): 
    return (s or "").strip()

def _mk_row(loc: dict):
    # essaie différents schémas courants renvoyés par Stockist
    name   = loc.get("name") or loc.get("title") or loc.get("store_name") or ""
    addr1  = loc.get("address1") or loc.get("street") or loc.get("address_line_1") or ""
    addr2  = loc.get("address2") or loc.get("address_line_2") or ""
    city   = loc.get("city") or ""
    state  = loc.get("state") or loc.get("province") or ""
    postal = loc.get("postal_code") or loc.get("postcode") or loc.get("zip") or ""
    country= loc.get("country") or loc.get("country_code") or ""
    website= loc.get("website") or loc.get("url") or ""

    # adresse pleine
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

def scrape_stockist(url: str):
    """
    Stratégie:
    1) Aller sur la page (sans attendre 'networkidle')
    2) Intercepter les réponses réseau provenant de stocki.st / stockist.* contenant du JSON
    3) Extraire la liste des magasins depuis ce JSON
    4) Fallback (si rien intercepté) : tenter une requête XHR lancée par le widget via le contenu de la frame
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--single-process","--no-zygote"],
        )
        context = browser.new_context(
            locale="fr-FR",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/123 Safari/537.36")
        )
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)
        page.set_default_timeout(NAV_TIMEOUT)

        captured = {"items": []}

        def handle_response(resp):
            try:
                u = resp.url.lower()
                ct = resp.headers.get("content-type","").lower()
                if ("stocki.st" in u or "stockist" in u) and ("json" in ct or u.endswith(".json")):
                    data = resp.json()
                    # différentes formes possibles
                    candidates = []
                    if isinstance(data, dict):
                        for k in ("locations","results","stores","items","data","payload"):
                            if k in data:
                                candidates.append(data[k])
                    if isinstance(data, list):
                        candidates.append(data)
                    # aplatis
                    out = []
                    for c in candidates:
                        if isinstance(c, list):
                            out.extend(c)
                        elif isinstance(c, dict):
                            # parfois data -> locations
                            for k in ("locations","results","stores","items"):
                                if k in c and isinstance(c[k], list):
                                    out.extend(c[k])
                    if out:
                        captured["items"] = out
            except Exception:
                pass

        # écouter TOUT le contexte (réponses venant aussi des iframes)
        context.on("response", handle_response)

        # aller sur la page
        page.goto(url, wait_until="domcontentloaded")

        # attendre un peu que le widget charge et émette la requête
        t0 = time.time()
        while (time.time() - t0) * 1000 < CAPTURE_WINDOW_MS and not captured["items"]:
            page.wait_for_timeout(300)

        # si toujours rien, essayer de trouver la frame stockist et forcer un petit scroll
        if not captured["items"]:
            try:
                target = None
                for f in page.frames:
                    u = (f.url or "").lower()
                    if "stocki.st" in u or "stockist" in u or "stockist.co" in u:
                        target = f; break
                if target:
                    for _ in range(10):
                        try:
                            target.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            page.wait_for_timeout(200)
                        except Exception:
                            break
                    # patienter encore un peu pour intercepter la requête JSON
                    t1 = time.time()
                    while (time.time() - t1) * 1000 < CAPTURE_WINDOW_MS and not captured["items"]:
                        page.wait_for_timeout(300)
            except Exception:
                pass

        browser.close()

    # Conversion
    rows = []
    for loc in captured["items"]:
        try:
            rows.append(_mk_row(loc))
        except Exception:
            continue

    # log dans Render
    print(f"[SCRAPER] {len(rows)} magasins capturés via JSON")
    return rows
