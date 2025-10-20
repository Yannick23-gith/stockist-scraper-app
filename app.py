#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, Response, send_file, redirect, url_for, flash
import io
import csv
from scraper import scrape_stockist

app = Flask(__name__)
app.secret_key = "change-this"

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/scrape", methods=["POST"])
def scrape():
    url = request.form.get("url", "").strip()
    if not url:
        flash("Merci d’indiquer une URL.", "error")
        return redirect(url_for("index"))
    try:
        results = scrape_stockist(url)
        # build CSV in-memory
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["name","address_full","street","city","postal_code","country","url"])
        writer.writeheader()
        for row in results:
            writer.writerow(row)
        output.seek(0)
        filename = "stores.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        # show error on page
        flash(f"Erreur: {e}", "error")
        return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
