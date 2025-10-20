# Use Python base
FROM python:3.11-slim

# Prevent interactive tzdata
ENV DEBIAN_FRONTEND=noninteractive

# System deps for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates curl unzip fonts-liberation libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libdrm2 libxkbcommon0 libgtk-3-0 libasound2 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# App dir
WORKDIR /app

# Copy files
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install browsers for Playwright
RUN python -m playwright install --with-deps chromium

COPY . /app

ENV PORT=8000
EXPOSE 8000

CMD ["python", "app.py"]
