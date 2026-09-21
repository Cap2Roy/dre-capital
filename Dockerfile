FROM python:3.13-slim

WORKDIR /app

# System deps for psycopg2 (Cloud SQL Postgres)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run provides PORT env; default 8080
ENV PORT=8080
EXPOSE 8080

# Cloud Run: init DB on startup (SQLite dev) — Cloud SQL handles schema in prod
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
