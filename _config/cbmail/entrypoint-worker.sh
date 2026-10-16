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

CELERY_APP="cbmail.workers.celery_app:celery_app"

# Две роли, два контейнера — см. docker-compose.yaml.
#
# Раньше рассылки и обслуживание делил один пул процессов, и обслуживание
# проигрывало: отправка занимает процесс на часы, поэтому при трёх идущих
# рассылках тик расписания переставал выполняться совсем. Новые кампании никто
# не доставал, зависшие никто не возвращал — сервис «вставал» целиком.
case "${WORKER_ROLE:-campaigns}" in
  campaigns)
    # Сколько кампаний уходит одновременно.
    #
    # Согласовано с числом ящиков на рассылку (MAX_PARALLEL_MAILBOXES): каждая
    # кампания держит до трёх соединений с базой плюс одно своё. При шести
    # процессах это до двадцати четырёх подключений с воркера — поднимая число,
    # поднимайте и пул в cbmail/db/session.py, и max_connections у Postgres.
    #
    # Лишние кампании теперь не теряются и не выглядят сломанными: они видны
    # как «В очереди» и уходят по мере освобождения мест.
    CONCURRENCY="${CELERY_CONCURRENCY:-6}"
    echo "Воркер рассылок, одновременно кампаний: $CONCURRENCY"
    exec celery -A "$CELERY_APP" worker \
      --queues campaigns --hostname "campaigns@%h" \
      -l info --concurrency="$CONCURRENCY"
    ;;
  maintenance)
    # Один процесс и собственный beat. Больше одного не нужно: тик короткий,
    # а два beat'а рассылали бы одни и те же кампании дважды.
    echo "Воркер обслуживания (расписание и сбор зависших)"
    exec celery -A "$CELERY_APP" worker \
      --queues maintenance --hostname "maintenance@%h" \
      --beat -l info --concurrency=1
    ;;
  *)
    echo "Неизвестная роль воркера: ${WORKER_ROLE}. Ожидается campaigns или maintenance." >&2
    exit 1
    ;;
esac
