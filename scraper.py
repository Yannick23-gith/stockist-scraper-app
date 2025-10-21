# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright
import re, time, json

NAV_TIMEOUT = 120_000
# Attendre plus longtemps pour laisser le temps à toutes les requêtes AJAX d'arriver
CAPTURE_WINDOW_MS = 60_000   # 60s

def scrape_stockist(url: str):
    """
    Stratégie JSON:
      - écouter toutes les réponses réseau (page + iframes)
      - agréger TOUTES les pages renvoyées par l'API Stockist
      - convertir en lignes (name, address, city, etc.)
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-gpu", "--single-process", "--no-zygote"
            ]
        )
        context = browser.new_context(
            locale="fr-FR",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/123 Safari/537.36")
        )
        page = context.new_page()

        # On agrège ici toutes les locations vues passer (toutes pages confondues)
        captured = []

        def handle_response(resp):
            """Intercepte les réponses JSON de Stockist (page + frames)."""
            try:
                u = (resp.url or "").lower()
                ct = resp.headers.get("content-type", "").lower()
                if (("stocki.st" in u) or ("stockist" in u)) and (("json" in ct) or u.endswith(".json")):
                    data = resp.json()
                    # Différents schémas possibles
                    if isinstance(data, dict):
                        for k in ("locations", "results", "stores", "items", "data", "payload"):
                            if k in data:
                                val = data[k]
                                if isinstance(val, list):
                                    captured.extend(val)
                                elif isinstance(val, dict):
                                    for kk in ("locations", "results", "stores", "items"):
                                        if kk in val and isinstance(val[kk], list):
                                            captured.extend(val[kk])
                    elif isinstance(data, list):
                        captured.extend(data)
            except Exception:
                pass

        # IMPORTANT: écouter au niveau du contexte (ça capte aussi les iframes)
        context.on("response", handle_response)

        # 1) Aller sur la page (pas de 'networkidle' qui bloque souvent)
        page.set_default_navigation_timeout(90_000)
        page.goto(url, wait_until="domcontentloaded")

        # 2) Fenêtre d'écoute: on attend jusqu'à 60s et on laisse l'API paginer
        t0 = time.time()
        while (time.time() - t0) * 1000 < CAPTURE_WINDOW_MS:
            page.wait_for_timeout(300)

        # 3) Si on n'a rien eu, on tente de stimuler la frame (scroll) puis on attend encore
        if not captured:
            try:
                frame = None
                for f in page.frames:
                    uu = (f.url or "").lower()
                    if "stocki.st" in uu or "stockist" in uu or "stockist.co" in uu:
                        frame = f
                        break
                if frame:
                    for _ in range(10):
                        try:
                            frame.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            page.wait_for_timeout(200)
                        except Exception:
                            break
                    t1 = time.time()
                    while (time.time() - t1) * 1000 < CAPTURE_WINDOW_MS:
                        page.wait_for_timeout(300)
                        if captured:
                            break
            except Exception:
                pass

        browser.close()

    # Conversion → lignes propres
    rows = []
    for loc in captured:
        try:
            rows.append(_mk_row(loc))
        except Exception:
            continue

    # Dédup’ simple (nom + adresse complète)
    seen = set()
    uniq = []
    for r in rows:
        key = (r["name"].lower(), r["address_full"].lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    print(f"[SCRAPER] {len(uniq)} magasins capturés (agrégés sur plusieurs pages JSON)")
    return uniq
