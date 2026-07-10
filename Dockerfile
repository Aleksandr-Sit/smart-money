# Контур 2 — монитор конфлюенс-сигналов (24/7 на VPS).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements-monitor.txt .
RUN pip install --no-cache-dir -r requirements-monitor.txt

COPY src/ ./src/
COPY config/ ./config/

# .env и output/ монтируются как volume (секреты + флоу-watchlist + логи), не копируются в образ.
CMD ["python", "-m", "src.monitor"]
