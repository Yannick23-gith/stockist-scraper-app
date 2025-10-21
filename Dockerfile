# Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Même avec l'image Playwright, on force l'install des binaires pour être sûr
RUN python -m playwright install --with-deps chromium

# (astuce) échouer le build si playwright n'est pas importable
RUN python -c "import playwright; print('playwright OK')"

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=10000
CMD ["gunicorn", "-w", "1", "-k", "gthread", "-b", "0.0.0.0:10000", "app:app", "--timeout", "120", "--threads", "8"]
