#!/bin/bash
set -e

echo "Ожидание PostgreSQL..."
python - <<'PY'
import os, socket, sys, time
host = os.environ.get("DJANGO_POSTGRES_HOST", "cb-mail-db")
port = int(os.environ.get("DJANGO_POSTGRES_PORT", 5432))
for _ in range(90):
    try:
        socket.create_connection((host, port), 2).close()
        sys.exit(0)
    except OSError:
        time.sleep(1)
print("PostgreSQL не поднялся вовремя", file=sys.stderr)
sys.exit(1)
PY
echo "PostgreSQL готов"

# Единственная аддитивная таблица — создаём, если её ещё нет.
python -m cbmail.db.init_session

exec uvicorn cbmail.web.main:app --host 0.0.0.0 --port 8000 --workers "${WEB_WORKERS:-2}"
