# Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=10000
# 1 worker gthread = +économe et compatible Playwright
CMD ["gunicorn", "-w", "1", "-k", "gthread", "-b", "0.0.0.0:10000", "app:app", "--timeout", "120", "--threads", "8"]
