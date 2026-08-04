"""Фоновые задачи рассылки.

Гейт лицензии здесь — часть kill-switch: при недействительном ключе рассылка
не идёт, а не только блокируется веб-интерфейс. Иначе остановленный по лицензии
сервис продолжал бы слать письма из очереди.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from cbmail.core.licensing import verify_license
from cbmail.db.session import sync_session_scope
from cbmail.services import campaign_sender
from cbmail.services.campaign_sender import CampaignNotSendable
from cbmail.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


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


@celery_app.task(name="cbmail.maintenance_tick")
def maintenance_tick() -> dict:
    """Раз в минуту: восстановить зависшие и разослать наступившие кампании."""
    ok, reason = verify_license(force=True)
    if not ok:
        logger.warning("Тик обслуживания пропущен: лицензия недействительна (%s)", reason)
        return {"skipped": "license", "reason": reason}

    now = datetime.now(timezone.utc)
    with sync_session_scope() as session:
        recovered = campaign_sender.recover_stuck_campaigns(session, now)
        due = campaign_sender.find_due_campaigns(session, now)

    for campaign_id in due:
        send_campaign_task.delay(campaign_id, force=False)

    return {"recovered": len(recovered), "dispatched": len(due)}
