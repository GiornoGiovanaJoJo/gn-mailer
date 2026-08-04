"""Celery-приложение.

Брокер и backend берутся из окружения (``CELERY_BROKER_URL``), а при его
отсутствии — из настроек Redis. Beat раз в минуту запускает единый тик
обслуживания: разослать наступившие кампании и вернуть в черновик зависшие.
"""

from __future__ import annotations

import os

from celery import Celery

from cbmail.core.config import get_settings


def _broker_url() -> str:
    return os.environ.get("CELERY_BROKER_URL") or get_settings().redis_url


def _backend_url() -> str:
    return os.environ.get("CELERY_RESULT_BACKEND") or get_settings().redis_url


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
    beat_schedule={
        "dispatch-and-recover-every-minute": {
            "task": "cbmail.maintenance_tick",
            "schedule": 60.0,
        }
    },
)

# Регистрируем задачи.
import cbmail.workers.tasks  # noqa: E402,F401
