"""Фоновые задачи рассылки.

Гейт лицензии здесь — часть kill-switch: при недействительном ключе рассылка
не идёт, а не только блокируется веб-интерфейс. Иначе остановленный по лицензии
сервис продолжал бы слать письма из очереди.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from celery.signals import worker_shutting_down

from cbmail.core.licensing import verify_license
from cbmail.db.session import sync_session_scope
from cbmail.services import campaign_sender
from cbmail.services.campaign_sender import CampaignNotSendable
from cbmail.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@worker_shutting_down.connect
def _stop_sending_gracefully(**_: object) -> None:
    """Выкатка: просим идущие рассылки остановиться на границе письма.

    Без этого контейнер убивал отправку на полуслове. Кампания оставалась в
    статусе «Отправляется» с половиной разосланных писем, и вернуть её в
    работу мог только сборщик зависших — через четверть часа после выкатки.
    Теперь рассылка сама просит повтор и продолжится сразу после перезапуска.
    """
    logger.info("Воркер останавливается — завершаем рассылки на границе письма")
    campaign_sender.request_shutdown()


@celery_app.task(name="cbmail.send_campaign")
def send_campaign_task(campaign_id: int, force: bool = False) -> dict:
    ok, reason = verify_license(force=True)
    if not ok:
        logger.warning("Отправка кампании %s пропущена: лицензия недействительна (%s)", campaign_id, reason)
        return {"skipped": "license", "reason": reason}

    with sync_session_scope() as session:
        try:
            result = campaign_sender.send_campaign(session, campaign_id, force=force)
        except CampaignNotSendable as exc:
            logger.info("Кампания %s не отправлена: %s", campaign_id, exc)
            return {"campaign_id": campaign_id, "error": str(exc)}
    return {
        "campaign_id": campaign_id,
        "sent": result.sent,
        "failed": result.failed,
        "stopped": result.stopped_by_user,
        "already_complete": result.already_complete,
    }


@celery_app.task(
    name="cbmail.maintenance_tick",
    # Тик обязан быть коротким. Своя очередь спасает его от очереди рассылок,
    # но не от самого себя: запрос, повисший на сетевом сбое, занял бы
    # единственный процесс обслуживания навсегда — и диспетчер снова умер бы,
    # теперь уже тихо. Рассылкам такой предел не ставим: кампания на сутки с
    # большим интервалом между письмами — это норма, а не зависание, и следит
    # за ними сборщик по отметке активности.
    soft_time_limit=120,
    time_limit=180,
)
def maintenance_tick() -> dict:
    """Раз в минуту: вернуть в расписание зависшие и разослать наступившие."""
    ok, reason = verify_license(force=True)
    if not ok:
        logger.warning("Тик обслуживания пропущен: лицензия недействительна (%s)", reason)
        return {"skipped": "license", "reason": reason}

    now = datetime.now(timezone.utc)
    with sync_session_scope() as session:
        recovered = campaign_sender.recover_stuck_campaigns(session, now)
        # Кампании уже помечены «В очереди» — в базе, одним запросом с отбором.
        # Поэтому следующий тик их не увидит, даже если очередь длинная и
        # задачи ждут свободного процесса час.
        due = campaign_sender.find_due_campaigns(session, now)

    for campaign_id in due:
        try:
            send_campaign_task.delay(campaign_id, force=False)
        except Exception:
            # Брокер недоступен. Кампания осталась «В очереди»; сборщик вернёт
            # её в расписание, и следующий тик поставит задачу заново.
            logger.warning(
                "Кампания %s помечена в очередь, но задача не поставлена", campaign_id, exc_info=True
            )

    return {"recovered": len(recovered), "dispatched": len(due)}
