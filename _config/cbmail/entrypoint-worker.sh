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

# Сколько кампаний уходит одновременно.
#
# Было 1: вторая запущенная рассылка просто ждала в очереди, пока первая не
# закончится — а это часы на большом списке. Со стороны человека она выглядела
# незапустившейся, и её запускали повторно.
#
# Три — согласовано с числом ящиков на рассылку (MAX_PARALLEL_MAILBOXES): каждая
# кампания держит до трёх соединений с базой плюс одно своё, то есть потолок
# примерно двенадцать подключений на воркер. Поднимать это число нужно вместе с
# размером пула в cbmail/db/session.py и max_connections у Postgres.
CONCURRENCY="${CELERY_CONCURRENCY:-3}"

exec celery -A cbmail.workers.celery_app:celery_app worker --beat -l info --concurrency="$CONCURRENCY"
