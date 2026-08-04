#!/bin/bash
set -e

echo "Ожидание PostgreSQL и Redis..."
python - <<'PY'
import os, socket, sys, time
targets = [
    (os.environ.get("DJANGO_POSTGRES_HOST", "cb-mail-db"), int(os.environ.get("DJANGO_POSTGRES_PORT", 5432))),
    (os.environ.get("DJANGO_REDIS_HOST", "cb-mail-redis"), int(os.environ.get("DJANGO_REDIS_PORT", 6379))),
]
for host, port in targets:
    for _ in range(90):
        try:
            socket.create_connection((host, port), 2).close()
            break
        except OSError:
            time.sleep(1)
    else:
        print(f"{host}:{port} не поднялся вовремя", file=sys.stderr)
        sys.exit(1)
PY
echo "Зависимости готовы"

exec celery -A cbmail.workers.celery_app:celery_app worker --beat -l info --concurrency=1
