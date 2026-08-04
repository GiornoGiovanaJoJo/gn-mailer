FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# libpq5 нужен psycopg (синхронный движок Celery-воркера и init_session).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Зависимости — отдельным слоем, чтобы не переустанавливались при правке кода.
COPY app/pyproject.toml /app/pyproject.toml
COPY app/cbmail /app/cbmail
RUN pip install .

COPY _config/cbmail/entrypoint-web.sh /app/entrypoint-web.sh
COPY _config/cbmail/entrypoint-worker.sh /app/entrypoint-worker.sh
RUN chmod +x /app/entrypoint-web.sh /app/entrypoint-worker.sh

EXPOSE 8000
