#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import io
import csv
from flask import Flask, render_template, request, Response, redirect, url_for, flash
from jinja2 import TemplateNotFound
from scraper import scrape_stockist

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# On force le dossier des templates pour éviter tout souci de résolution
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.secret_key = "change-this"

@app.route("/", methods=["GET"])
def index():
    try:
        return render_template("index.html")
    except TemplateNotFound:
        # Fallback au cas où le template n'est pas trouvé
        return (
            "<h1>Stockist Scraper</h1>"
            "<p>Form POST → <code>/scrape</code> avec <code>url=https://…</code></p>",
            200,
        )

@app.route("/scrape", methods=["POST"])
def scrape():
    url = request.form.get("url", "").strip()
    if not url:
        flash("Merci d’indiquer une URL.", "error")
        return redirect(url_for("index"))
    try:
        results = scrape_stockist(url)
        # Log utile dans les logs Render
        print(f"[SCRAPER] {len(results)} magasins trouvés pour {url}")
        if not results:
            flash("Aucun point de vente détecté. Réessaie dans 10–15s (réveil de l’instance) ou vérifie l’URL.", "error")
            return redirect(url_for("index"))

        import io, csv
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["name","address_full","street","city","postal_code","country","url"])
        writer.writeheader()
        for row in results:
            writer.writerow(row)
        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=stores.csv"}
        )
    except Exception as e:
        flash(f"Erreur: {e}", "error")
        return redirect(url_for("index"))

@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

if __name__ == "__main__":
    # Render injecte PORT ; si non présent, on tombe sur 8000
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
