# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import re

NAV_TIMEOUT = 90000      # 90s pour laisser le temps au réveil/render
WAIT_SELECTORS_MS = 60000
LIST_SELECTORS = [".st-list", ".stockist-list", "[class*='st-list__container']"]
ITEM_SELECTORS = [".st-list__item", ".stockist-location", "[class*='st-list__item']", "[data-testid='location-list-item']"]
NAME_SELECTORS = [".st-list__name", ".stockist-location__name", "[class*='name']"]
ADDR_SELECTORS = [".st-list__address", ".stockist-location__address", "address", "[class*='address']"]
URL_SELECTORS  = ["a[href*='http']:not([href*='google'])", "a[href^='http']"]

def _split_address(text: str):
    if not text: return "", "", "", "", ""
    t = re.sub(r"\s+", " ", text).strip(" ,;\n\t")
    street = city = postal = country = ""
    m = re.search(r"(\b\d{4,5}\b)", t)
    if m: postal = m.group(1)
    parts = [p.strip() for p in re.split(r"[,\.;]\s*", t) if p.strip()]
    if parts:
        last = parts[-1].lower()
        if any(k in last for k in ["france","belgique","suisse","italie","spain","espagne","portugal","germany","allemagne","uk","united kingdom","ireland","pays-bas","netherlands"]):
            country = parts.pop(-1)
    if postal:
        for p in parts[::-1]:
            if postal in p:
                city = p.replace(postal, "").strip(" ,"); break
    if not city and len(parts) >= 2: city = parts[-1]
    if parts: street = parts[0]
    return t, street, city, postal, country

def _parse_items(html: str):
    soup = BeautifulSoup(html, "lxml")
    list_node = None
    for sel in LIST_SELECTORS:
        list_node = soup.select_one(sel)
        if list_node: break
    scope = list_node or soup
    items = []
    for item_sel in ITEM_SELECTORS:
        for it in scope.select(item_sel):
            name = None
            for ns in NAME_SELECTORS:
                el = it.select_one(ns)
                if el and el.get_text(strip=True):
                    name = el.get_text(" ", strip=True); break
            if not name:
                fb = it.select_one("h3, h2, strong")
                if fb: name = fb.get_text(" ", strip=True)
            addr = None
            for asel in ADDR_SELECTORS:
                ael = it.select_one(asel)
                if ael and ael.get_text(strip=True):
                    addr = ael.get_text(" ", strip=True); break
            url = ""
            for us in URL_SELECTORS:
                link = it.select_one(us)
                if link and link.has_attr("href"):
                    url = link["href"]; break
            if name or addr or url:
                full, street, city, cp, country = _split_address(addr or "")
                items.append({
                    "name": name or "",
                    "address_full": full,
                    "street": street,
                    "city": city,
                    "postal_code": cp,
                    "country": country,
                    "url": url
                })
    # dedup
    seen, uniq = set(), []
    for d in items:
        key = (d["name"].lower(), d["address_full"].lower())
        if key in seen: continue
        seen.add(key); uniq.append(d)
    return uniq

def _try_accept_cookies(page_like):
    sels = [
        "button:has-text('Tout accepter')", "button:has-text('Accepter')",
        "#onetrust-accept-btn-handler", "button#didomi-notice-agree-button",
        "button:has-text(\"J’accepte\")", "button:has-text('Accept all')"
    ]
    for s in sels:
        try:
            el = page_like.locator(s)
            if el and el.is_visible(): el.click()
        except Exception:
            pass

def _load_all_locations(page_like):
    # “Load more” / “Voir plus”
    for _ in range(15):
        try:
            btn = page_like.locator("button:has-text('Load more'), button:has-text('Voir plus'), .st-list__load-more button")
            if btn and btn.is_visible(): btn.click()
            else: break
        except Exception:
            break
    # auto-scroll
    last = 0
    for _ in range(25):
        try: page_like.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception: break
        page_like.wait_for_timeout(300)
        try: h = page_like.evaluate("document.body.scrollHeight")
        except Exception: break
        if h == last: break
        last = h

def scrape_stockist(url: str):
    with sync_playwright() as p:
browser = p.chromium.launch(
    headless=True,
    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process", "--no-zygote"]
)

        context = browser.new_context(
            locale="fr-FR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
        )
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)
        page.set_default_timeout(NAV_TIMEOUT)

        # ⚠️ ne plus attendre 'networkidle' (sources analytics empêchent l'idle)
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        _try_accept_cookies(page)

        # tente d'abord sur la page principale
        frame = None
        try:
            page.wait_for_selector(".st-list__item, .stockist-location, .st-list", timeout=WAIT_SELECTORS_MS)
        except Exception:
            # si rien, on cherche une iframe Stockist
            for f in page.frames:
                try:
                    if f.url and ("stocki.st" in f.url or "stockist" in f.url or "stockist.co" in f.url):
                        frame = f; break
                except Exception:
                    pass
            if frame:
                _try_accept_cookies(frame)
                frame.wait_for_selector(".st-list__item, .stockist-location, .st-list", timeout=WAIT_SELECTORS_MS)

        target = frame if frame else page
        _load_all_locations(target)

        html = target.content()
        browser.close()

    return _parse_items(html)
