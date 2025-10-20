# Stockist Store Locator Scraper (Flask + Playwright)

Une mini‑app web pour extraire **Nom / Adresse / Ville / Code postal / Pays / URL** depuis une page **Stockist** (ex: https://pieceandlove.fr/pages/distributeurs) et télécharger le résultat en **CSV**.

## 1) Lancer en local (sans Docker)
```bash
python -m venv venv && source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install
python app.py
# ouvre http://localhost:8000
```

## 2) Lancer avec Docker (recommandé)
```bash
docker build -t stockist-scraper .
docker run -p 8000:8000 stockist-scraper
# ouvre http://localhost:8000
```

## 3) Déployer sur Render / Railway / Fly.io
**Option Docker (simple)** : push ce repo sur GitHub puis crée un service web Docker.
- Port: `8000`
- Health check (optionnel): `/`

## Remarques
- Conçu pour store locators **Stockist** (classes `.st-list__item`, etc.).
- Respecte les CGU/robots.txt. Usage raisonnable (une page, une extraction).
- Si tes concurrents utilisent d’autres widgets, adapte `scraper.py` (sélecteurs).
