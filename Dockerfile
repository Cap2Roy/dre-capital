FROM python:3.13-slim

WORKDIR /app

# System deps for psycopg2 (Cloud SQL Postgres) + bcrypt
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run provides PORT env; default 8080
ENV PORT=8080
EXPOSE 8080

# Startup: init DB tables + seed admin user if none exist, then serve
CMD python -c "from app.database import init_db; from app.seed import ensure_admin; init_db(); ensure_admin()" && \
    uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
