# ---------- scraper.py : remplacement complet des helpers d'extraction ----------

import re
import requests
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, Tuple

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
}

def _log(msg: str) -> None:
    # utilitaire pour voir ce qui se passe dans les logs Render
    print(f"[stockist] {msg}", flush=True)

# 1) Détecteurs “rapides” sur texte HTML simple (requests)
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

# 2) Fallback Playwright + écoute réseau + DOM + variables globales
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
            bypass_csp=True,  # laisse tourner du JS même avec CSP strictes
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

        # écoute toutes les requêtes pour attraper l’ID
        page.on("request", sniff_request)

        await page.goto(url, wait_until="networkidle", timeout=45_000)

        # petit délai pour laisser Stockist initialiser
        await page.wait_for_timeout(1500)

        if found["sid"]:
            return found["sid"]

        # 2.a DOM : attributs data-*
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

        # 2.b Variables JS globales usuelles
        candidates = [
            "window.stockist",
            "window.stockistSettings",
            "window.stockistConfig",
            "window.__STOCKIST__",
            "window.__STOCKIST_CONFIG__",
        ]
        for js_name in candidates:
            try:
                val = await page.evaluate(f"(() => {{ try {{ return {js_name}; }} catch (e) {{ return null; }} }})()")
                if not val:
                    continue
                txt = str(val)
                sid = _extract_store_id_from_text(txt)
                if sid:
                    _log(f"store_id trouvé dans variable JS {js_name} : {sid}")
                    return sid
            except Exception:
                pass

        # 2.c Dernière chance : tout le HTML rendu
        html = await page.content()
        sid = _extract_store_id_from_text(html)
        if sid:
            _log(f"store_id trouvé dans HTML rendu (Playwright) : {sid}")
            return sid

        return None

def _extract_store_id(input_value: str) -> Tuple[str, Optional[str]]:
    """
    Retourne (store_id, referer).
    - Si input = ID numérique, on le renvoie direct.
    - Sinon: requests -> Playwright réseau/DOM/JS -> HTML rendu.
    """
    value = (input_value or "").strip()

    if re.fullmatch(r"\d{5,}", value):
        _log(f"Entrée semble être un ID direct: {value}")
        return value, None

    # Tentative rapide
    sid = _extract_store_id_fast_html(value)
    if sid:
        return sid, value

    # Fallback complet Playwright
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
# ---------- FIN remplacement ----------
