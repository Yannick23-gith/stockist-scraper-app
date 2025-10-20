# Image officielle Playwright + Python + navigateurs (Chromium déjà prêt)
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Dossier de l'app
WORKDIR /app

# Dépendances Python
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Code
COPY . /app

# Port Flask
ENV PORT=8000
EXPOSE 8000

# Lancer l'app
CMD ["python", "app.py"]
