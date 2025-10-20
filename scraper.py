# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import time, re

WAIT_MS = 12000

LIST_SELECTORS = [".st-list", ".stockist-list", "[class*='st-list__container']"]
ITEM_SELECTORS = [".st-list__item", ".stockist-location", "[class*='st-list__item']", "[data-testid='location-list-item']"]
NAME_SELECTORS = [".st-list__name", ".stockist-location__name", "[class*='name']"]
ADDR_SELECTORS = [".st-list__address", ".stockist-location__address", "address", "[class*='address']"]
URL_SELECTORS  = ["a[href*='http']:not([href*='google'])", "a[href^='http']"]

def _split_address(text: str):
    if not text:
        return "", "", "", "", ""
    t = re.sub(r"\s+", " ", text).strip(" ,;\n\t")
    street = city = postal = country = ""
    m = re.search(r"(\b\d{4,5}\b)", t)
    if m:
        postal = m.group(1)
    parts = [p.strip() for p in re.split(r"[,\.;]\s*", t) if p.strip()]
    if parts:
        last = parts[-1].lower()
        if any(k in last for k in ["france","belgique","suisse","italie","spain","espagne","portugal","germany","allemagne","uk","united kingdom","ireland","pays-bas","netherlands"]):
            country = parts.pop(-1)
    if postal:
        for p in parts[::-1]:
            if postal in p:
                city = p.replace(postal, "").strip(" ,")
                break
    if not city and len(parts) >= 2:
        city = parts[-1]
    if parts:
        street = parts[0]
    return t, street, city, postal, country

def _parse_items(html: str):
    soup = BeautifulSoup(html, "lxml")
    list_node = None
    for sel in LIST_SELECTORS:
        list_node = soup.select_one(sel)
        if list_node:
            break
    scope = list_node or soup
    items = []
    for item_sel in ITEM_SELECTORS:
        for it in scope.select(item_sel):
            name = None
            for ns in NAME_SELECTORS:
                el = it.select_one(ns)
                if el and el.get_text(strip=True):
                    name = el.get_text(" ", strip=True)
                    break
            if not name:
                fallback = it.select_one("h3, h2, strong")
                if fallback:
                    name = fallback.get_text(" ", strip=True)
            addr = None
            for asel in ADDR_SELECTORS:
                ael = it.select_one(asel)
                if ael and ael.get_text(strip=True):
                    addr = ael.get_text(" ", strip=True)
                    break
            url = ""
            for us in URL_SELECTORS:
                link = it.select_one(us)
                if link and link.has_attr("href"):
                    url = link["href"]
                    break
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
    seen = set()
    uniq = []
    for d in items:
        key = (d["name"].lower(), d["address_full"].lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(d)
    return uniq

def scrape_stockist(url: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        found = False
        start = time.time()
        while (time.time() - start) * 1000 < WAIT_MS:
            html = page.content()
            if any(s in html for s in ["st-list__item", "stockist-location", "st-list__name"]):
                found = True
                break
            page.wait_for_timeout(300)
        html = page.content()
        browser.close()
    return _parse_items(html)
