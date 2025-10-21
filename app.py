# app.py
import csv
import io
import datetime
from flask import Flask, request, render_template, jsonify, Response
from scraper import scrape_stockist

app = Flask(__name__)

# Page d'accueil avec le formulaire
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

# Endpoint scraping : accepte POST (formulaire) ET GET (tests via /scrape?url=...)
@app.route("/scrape", methods=["GET", "POST"])
def scrape():
    # request.values marche pour GET et POST (args + form)
    url = (request.values.get("url") or "").strip()

    if not url:
        # On ré-affiche l'UI avec un message d’erreur clair
        return render_template(
            "index.html",
            messages=[("error", "Merci de saisir l’URL du store locator ou l’ID Stockist (ex: 12345).")]
        ), 400

    try:
        rows = scrape_stockist(url)
    except Exception as e:
        app.logger.exception("Scrape failed")
        return render_template("index.html", messages=[("error", f"Erreur: {e}")]), 500

    # Option pratique : /scrape?url=...&format=json pour voir le résultat brut
    if request.args.get("format") == "json":
        return jsonify(rows)

    # Génération CSV
    fieldnames = [
        "name", "address1", "address2", "city", "state", "postal_code",
        "country", "phone", "website", "lat", "lng", "address_full"
    ]
    sio = io.StringIO()
    writer = csv.DictWriter(sio, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in fieldnames})

    csv_bytes = sio.getvalue().encode("utf-8-sig")
    filename = f"stores_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    headers = {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return Response(csv_bytes, headers=headers)

if __name__ == "__main__":
    # Port 8000 en local ; Render utilisera le port via Docker/Procfile
    app.run(host="0.0.0.0", port=8000)
