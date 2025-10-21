# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright
import re, time, json
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

NAV_TIMEOUT = 120_000
CAPTURE_WINDOW_MS = 45_000
MAX_PAGES = 250

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
    p = urlparse(u)
    q = parse_qs(p.query)
    q[key] = [str(value)]
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse(p._replace(query=new_q))

def _parse_json_or_jsonp(resp):
    """
    Renvoie (is_json, data) où data est un dict/list si parse OK.
    Supporte JSON pur ou JSONP: callback_name({...});
    """
    try:
        ct = resp.headers.get("content-type","").lower()
        body = resp.text()  # text pour gérer JSONP
        # JSON direct ?
        if "json" in ct or body.strip().startswith(("{","[")):
            return True, resp.json()
        # JSONP / javascript
        if "javascript" in ct or "(" in body:
            m = re.search(r"\(\s*({[\s\S]*}|[\[\s\S]*?)\)\s*;?\s*$", body)
            if m:
                return True, json.loads(m.group(1))
    except Exception:
        pass
    return False, None

def scrape_stockist(url: str):
    """
    Stratégie robuste :
      A) Essayer de détecter l'ID de boutique Stockist dans le HTML
         (data-stockist-store, configs embarquées...)
         Puis paginer via l'API officielle : .../api/stores/{store_id}/locations
      B) Si on ne trouve pas l'ID, fallback sur l'ancienne méthode :
         on écoute les réponses réseau et on reconstruit la pagination.
    """
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

        # -----------------------
        # A) Tenter HTML -> store_id
        # -----------------------
        page.goto(url, wait_until="domcontentloaded")

                store_id = None
        try:
            # 0) Essayer via les <script src="...stockist... ?store=12345">
            scripts = page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
            for s in scripts:
                sl = (s or "").lower()
                if "stockist" in sl and "store=" in sl:
                    m = re.search(r"[?&]store=(\d+)", s)
                    if m:
                        store_id = m.group(1)
                        print(f"[SCRAPER] store_id via script src: {store_id}")
                        break

            # 1) Sinon, attributs HTML usuels
            if not store_id:
                html = page.content()

                m = re.search(r'data-stockist-store=["\'](\d+)["\']', html, flags=re.I)
                if not m:
                    m = re.search(r'"store_id"\s*:\s*(\d+)', html, flags=re.I)
                if not m:
                    # window.stockistConfig = { store_id: 12345, ... }
                    m = re.search(r'stockist[^=]*=\s*{[^}]*store[_\s]*id["\']?\s*[:=]\s*("?)(\d+)\1', html, flags=re.I)

                if m and not store_id:
                    store_id = m.group(1) if m.lastindex == 1 else (m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(0))
                    print(f"[SCRAPER] store_id via HTML: {store_id}")
        except Exception as e:
            print(f"[SCRAPER] store_id parse error: {e}")

        rows = []

        def api_paginate_store(store_id_val: str):
            """Pagine via endpoints officiels Stockist pour un store_id donné."""
            local_rows = []
            api = context.request

            # Endpoints les plus courants (on en teste plusieurs)
            endpoints = [
                f"https://app.stockist.co/api/stores/{store_id_val}/locations",
                f"https://stocki.st/api/stores/{store_id_val}/locations",
                f"https://stockist.co/api/stores/{store_id_val}/locations",
            ]

            seen_ids = set()
            for base in endpoints:
                try:
                    per_page = 100
                    page_num = 1
                    got_any = False
                    while page_num <= MAX_PAGES:
                        page_url = _set_query_param(_set_query_param(base, "per_page", per_page), "page", page_num)
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
                        if data is None:
                            break
                        locs = _extract_locations(data)
                        print(f"[SCRAPER] page {page_num}: {len(locs)} items")
                        if not locs:
                            break
                        got_any = True
                        for loc in locs:
                            loc_id = loc.get("id") or loc.get("location_id") or json.dumps(loc, sort_keys=True)[:80]
                            if loc_id in seen_ids:
                                continue
                            seen_ids.add(loc_id)
                            local_rows.append(_mk_row(loc))
                        page_num += 1
                        time.sleep(0.1)
                    if got_any:
                        return local_rows
                except Exception as e:
                    print(f"[SCRAPER] endpoint failed {base}: {e}")
                    continue
            return local_rows

        # Si on a l'ID, on passe directement par l'API
        if store_id and str(store_id).isdigit():
            rows = api_paginate_store(str(store_id))

        # -----------------------
        # B) Fallback : interception réseau si pas d'ID / pas de résultats
        # -----------------------
        if not rows:
            print("[SCRAPER] Fallback: interception réseau/pagination générique…")

            first_json_url = {"url": None}
            def handle_response(resp):
                try:
                    u = (resp.url or "")
                    lu = u.lower()
                    if ("stockist" in lu or "stocki.st" in lu):
                        ct = resp.headers.get('content-type','')
                        print(f"[SCRAPER][CANDIDATE] {resp.status} {u} CT={ct}")
                        if first_json_url["url"] is None and any(k in lu for k in ("location","store","graphql","api","search")):
                            first_json_url["url"] = u
                            print(f"[SCRAPER] first JSON URL (candidate): {u}")
                except Exception:
                    pass

            context.on("response", handle_response)

            # petite fenêtre d’écoute
            t0 = time.time()
            while (time.time() - t0) * 1000 < CAPTURE_WINDOW_MS and not first_json_url["url"]:
                page.wait_for_timeout(250)

            if not first_json_url["url"]:
                # stimuler la frame
                try:
                    for f in page.frames:
                        uu = (f.url or "").lower()
                        if "stockist" in uu or "stocki.st" in uu:
                            for _ in range(12):
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

            # pagination générique à partir de la première URL vue
            if first_json_url["url"]:
                api = context.request
                base = first_json_url["url"]
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
                    if data is None:
                        break
                    locs = _extract_locations(data)
                    print(f"[SCRAPER] page {page_num}: {len(locs)} items")
                    if not locs:
                        break
                    for loc in locs:
                        loc_id = loc.get("id") or loc.get("location_id") or json.dumps(loc, sort_keys=True)[:80]
                        if loc_id in seen_ids:
                            continue
                        seen_ids.add(loc_id)
                        rows.append(_mk_row(loc))
                    page_num += 1
                    time.sleep(0.12)
            else:
                print("[SCRAPER] Aucune URL JSON capturée (widget non détecté).")

        browser.close()

    # Dédup & sortie
    seen, out = set(), []
    for r in rows:
        k = (r["name"].lower(), r["address_full"].lower())
        if k in seen:
            continue
        seen.add(k); out.append(r)

    print(f"[SCRAPER] TOTAL: {len(out)} magasins (après pagination & dédup)")
    return out
