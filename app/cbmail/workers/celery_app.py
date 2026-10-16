"""Celery-приложение.

Брокер и backend берутся из окружения (``CELERY_BROKER_URL``), а при его
отсутствии — из настроек Redis. Beat раз в минуту запускает единый тик
обслуживания: разослать наступившие кампании и вернуть в расписание зависшие.

**Две очереди, и это главное в этом файле.** Раньше тик обслуживания был
обычной задачей в общей очереди и исполнялся тем же пулом процессов, что и
сами рассылки. Отправка держит процесс часами — пауза между письмами сделана
ожиданием внутри задачи. Как только рассылок становилось столько же, сколько
процессов в пуле, тик переставал выполняться ВООБЩЕ: новые кампании никто не
доставал из расписания, зависшие никто не возвращал. Снаружи это выглядело
так, что сервис перестал работать целиком, и чинилось только перезапуском.

Теперь ``campaigns`` разбирает один воркер, ``maintenance`` — другой, со своим
процессом и своим beat. Занятость рассылок на диспетчер больше не влияет.
"""

from __future__ import annotations

import os

from celery import Celery

from cbmail.core.config import get_settings


def _broker_url() -> str:
    return os.environ.get("CELERY_BROKER_URL") or get_settings().redis_url


def _backend_url() -> str:
    return os.environ.get("CELERY_RESULT_BACKEND") or get_settings().redis_url


#: Очередь самих рассылок — задачи на часы.
QUEUE_CAMPAIGNS = "campaigns"
#: Очередь обслуживания — короткие задачи, которые обязаны идти всегда.
QUEUE_MAINTENANCE = "maintenance"

celery_app = Celery("cbmail", broker=_broker_url(), backend=_backend_url())
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=get_settings().timezone,
    enable_utc=True,
    worker_hijack_root_logger=False,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue=QUEUE_CAMPAIGNS,
    task_routes={
        "cbmail.send_campaign": {"queue": QUEUE_CAMPAIGNS},
        "cbmail.maintenance_tick": {"queue": QUEUE_MAINTENANCE},
    },
    beat_schedule={
        "dispatch-and-recover-every-minute": {
            "task": "cbmail.maintenance_tick",
            "schedule": 60.0,
            "options": {"queue": QUEUE_MAINTENANCE},
        }
    },
)

# Регистрируем задачи.
import cbmail.workers.tasks  # noqa: E402,F401
