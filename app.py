import os, io, csv
from flask import Flask, render_template, request, Response, redirect, url_for, flash
from jinja2 import TemplateNotFound
from scraper import scrape_stockist

app = Flask(__name__)
app.secret_key = "change-this"

@app.route("/health")
def health():
    return "ok", 200

@app.route("/")
def index():
    try:
        return render_template("index.html")
    except TemplateNotFound:
        return "<h1>Stockist Scraper</h1><p>POST /scrape avec url=…</p>", 200

@app.route("/scrape", methods=["POST"])
def scrape():
    url = (request.form.get("url") or "").strip()
    if not url:
        flash("Merci d’indiquer une URL.", "error")
        return redirect(url_for("index"))
    try:
        results = scrape_stockist(url)
        print(f"[SCRAPER] {len(results)} magasins trouvés pour {url}")  # log Render
        if not results:
            flash("Aucun point de vente détecté. Réessaie ou vérifie l’URL.", "error")
            return redirect(url_for("index"))

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
