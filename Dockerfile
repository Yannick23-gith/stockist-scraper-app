# Image Playwright officielle (tu l'avais déjà)
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copie le code
COPY . /app

# --- fin du Dockerfile ---

ENV PORT=8000
ENV WEB_CONCURRENCY=1
EXPOSE 8000

# Lancer Gunicorn (forme shell pour que $PORT soit expandé)
CMD bash -lc 'gunicorn -w $WEB_CONCURRENCY -k gthread -t 300 -b 0.0.0.0:$PORT app:app'
