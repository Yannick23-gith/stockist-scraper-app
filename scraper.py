# scraper.py
import re
import json
import time
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, Tuple, List, Dict

import requests

# ---------------------------------------------------------------------
# Logging util
# ---------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[stockist] {msg}", flush=True)

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
}

# ---------------------------------------------------------------------
# 1) Extraction robuste du store_id (HTML -> Playwright réseau/DOM/JS)
# ---------------------------------------------------------------------
_PATTERNS_HTML = [
    r'data-stockist-store=["\'](\d+)["\']',
    r'data-stockist_store_id=["\'](\d+)["\']',
    r'data-store-id=["\'](\d+)["\']',
    r'stockist_store_id\s*[:=]\s*["\']?(\d+)',
    r'storeId\s*[:=]\s*["\']?(\d+)',
    r'stockist\s*=\s*{[^}]*store_id\s*:\s*(\d+)',
    r'stockistSettings\s*=\s*{[^}]*store_id\s*:\s*(\d+)',
    r'window\.stockistConfig\s*=\s*{[^}]*store_id\s*[:=]\s*["\']?(\d+)',
    r'stockist-api\.stockist\.co/[^"\']*?/(\d+)/locations',
    r'stockist\.co/[^"\']*?/(\d+)/locations',
    r'stockist\.co/[^"\']*?store_id=(\d+)',
    r'storelocator\.stockist\.co/[^"\']*?store_id=(\d+)',
    r'embed\.js[^"\']*?(?:\?|&)store_id=(\d+)',
    r'<(?:script|iframe)[^>]+src=["\'][^"\']*(?:store_id|storeId)=(\d+)',
]

def _extract_store_id_from_text(text: str) -> Optional[str]:
    for pat in _PATTERNS_HTML:
        m = re.search(pat, text, re.I | re.S)
        if m:
            return m.group(1)
    return None

def _extract_store_id_fast_html(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        sid = _extract_store_id_from_text(r.text)
        if sid:
            _log(f"store_id trouvé dans HTML (requests) : {sid}")
        return sid
    except Exception as e:
        _log(f"requests GET a échoué: {e}")
        return None

@asynccontextmanager
async def _browser():
    from playwright.async_api import async_playwright
    p = await async_playwright().start()
    try:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent=UA["User-Agent"],
            viewport={"width": 1280, "height": 900},
            bypass_csp=True,
        )
        yield context
    finally:
        try:
            await context.close()
        except Exception:
            pass
        await p.stop()

async def _extract_store_id_playwright(url: str) -> Optional[str]:
    from playwright.async_api import Page

    found: dict = {"sid": None}

    def sniff_request(req):
        if found["sid"]:
            return
        u = req.url
        if "stockist" in u and ("locations" in u or "store_id" in u):
            sid = _extract_store_id_from_text(u)
            if sid:
                found["sid"] = sid
                _log(f"store_id trouvé via requête réseau : {sid} ({u})")

    async with _browser() as ctx:
        page: Page = await ctx.new_page()
        page.on("request", sniff_request)

        await page.goto(url, wait_until="networkidle", timeout=45_000)
        await page.wait_for_timeout(1500)

        if found["sid"]:
            return found["sid"]

        # DOM (attributs)
        sel_list = [
            "[data-stockist-store]",
            "[data-stockist_store_id]",
            "[data-store-id]",
            "script[src*='stockist']",
            "iframe[src*='stockist']",
        ]
        for sel in sel_list:
            try:
                el = await page.query_selector(sel)
                if not el:
                    continue
                attrs = await el.evaluate("e => ({...e.dataset, src: e.src || ''})")
                for v in attrs.values():
                    if isinstance(v, str):
                        sid = _extract_store_id_from_text(v)
                        if sid:
                            _log(f"store_id trouvé dans DOM via {sel} : {sid}")
                            return sid
            except Exception:
                pass

        # Variables globales
        candidates = [
            "window.stockist",
            "window.stockistSettings",
            "window.stockistConfig",
            "window.__STOCKIST__",
            "window.__STOCKIST_CONFIG__",
        ]
        for js_name in candidates:
            try:
                val = await page.evaluate(
                    f"(() => {{ try {{ return {js_name}; }} catch (e) {{ return null; }} }})()"
                )
                if not val:
                    continue
                txt = str(val)
                sid = _extract_store_id_from_text(txt)
                if sid:
                    _log(f"store_id trouvé dans variable JS {js_name} : {sid}")
                    return sid
            except Exception:
                pass

        # HTML rendu
        html = await page.content()
        sid = _extract_store_id_from_text(html)
        if sid:
            _log(f"store_id trouvé dans HTML rendu (Playwright) : {sid}")
            return sid

        return None

def _extract_store_id(input_value: str) -> Tuple[str, Optional[str]]:
    value = (input_value or "").strip()

    # ID direct ?
    if re.fullmatch(r"\d{5,}", value):
        _log(f"Entrée = ID direct : {value}")
        return value, None

    # Essai rapide (requests)
    sid = _extract_store_id_fast_html(value)
    if sid:
        return sid, value

    # Fallback Playwright complet
    try:
        sid = asyncio.run(_extract_store_id_playwright(value))
    except RuntimeError as e:
        _log(f"Playwright runtime error: {e}")
        sid = None
    except Exception as e:
        _log(f"Playwright exception: {e}")
        sid = None

    if sid:
        return sid, value

    raise RuntimeError("Impossible de déterminer le store_id Stockist depuis la page.")

# ---------------------------------------------------------------------
# 2) Récupération des points de vente via l’API JSON de Stockist
# ---------------------------------------------------------------------
def _fetch_stockist_page(session: requests.Session, store_id: str, page: int, per: int) -> List[Dict]:
    """
    Essaye quelques variantes d’URL JSON de Stockist, retourne la liste d’objets "locations".
    """
    # Variantes d’API rencontrées selon les intégrations
    urls = [
        f"https://stockist.co/api/locations?store_id={store_id}&page={page}&per={per}",
        f"https://stockist.co/api/locations?store_id={store_id}&page={page}&per_page={per}",
        f"https://stockist-api.stockist.co/api/locations?store_id={store_id}&page={page}&per={per}",
    ]
    for u in urls:
        r = session.get(u, timeout=30)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        try:
            data = r.json()
        except json.JSONDecodeError:
            continue

        # Quelques structures possibles
        items = (data.get("locations")
                 or data.get("data")
                 or data.get("results")
                 or data if isinstance(data, list) else [])

        if items:
            _log(f"JSON reçu ({len(items)} éléments) via {u}")
            return items

    return []

def _normalize_item(it: Dict) -> Dict:
    return {
        "name": it.get("name"),
        "address": ", ".join(filter(None, [
            it.get("address1") or it.get("address_1"),
            it.get("address2") or it.get("address_2"),
            it.get("city"),
            it.get("state") or it.get("region"),
            it.get("postal_code") or it.get("postalCode"),
            it.get("country")
        ])),
        "lat": it.get("latitude") or it.get("lat"),
        "lng": it.get("longitude") or it.get("lng"),
        "phone": it.get("phone"),
        "website": it.get("website") or it.get("url"),
        "country": it.get("country"),
        "raw": it,  # pour debug si besoin
    }

# ---------------------------------------------------------------------
# 3) Fonction attendue par app.py : scrape_stockist(...)
# ---------------------------------------------------------------------
def scrape_stockist(input_value: str,
                    per_page: int = 200,
                    max_pages: int = 999) -> List[Dict]:
    """
    Retourne une liste de dicts normalisés (toutes les boutiques).
    `input_value` = URL du store locator OU ID numérique Stockist.
    """
    store_id, referer = _extract_store_id(input_value)
    _log(f"Scrape store_id={store_id} (referer={referer})")

    sess = requests.Session()
    headers = dict(UA)
    if referer:
        headers["Referer"] = referer
    sess.headers.update(headers)

    all_rows: List[Dict] = []
    page = 1
    while page <= max_pages:
        items = _fetch_stockist_page(sess, store_id, page, per_page)
        if not items:
            break
        all_rows += [_normalize_item(it) for it in items]

        if len(items) < per_page:
            break
        page += 1
        # tiny pause to be nice
        time.sleep(0.15)

    _log(f"Total ramené: {len(all_rows)}")
    return all_rows
