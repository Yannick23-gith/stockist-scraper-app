# Image Playwright officielle (tu l'avais déjà)
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copie le code
COPY . /app

# Expose & PORT
ENV PORT=8000
EXPOSE 8000

# 🚀 Lance Gunicorn (le Procfile est ignoré en mode Docker)
CMD gunicorn -w 2 -k gthread -t 240 -b 0.0.0.0:${PORT} app:app
