"""Почему рассылка не уходит — ответ одной командой.

Вопрос «кампания висит, что с ней?» возникает у поддержки чаще любого другого,
а ответ на него раньше собирался руками: статус в интерфейсе, причина в
журнале, квота ящика в третьем месте, занятость воркера — вообще нигде. При
этом все четыре состояния выглядят одинаково: «не отправляется».

Скрипт печатает их рядом. Разбирать нужно сверху вниз: первая же строка с
пометкой обычно и есть ответ.

Запуск на сервере (файл кладёт деплой, внутрь образа он не попадает):
    docker cp _config/campaigns-status.py cbmail-web:/tmp/campaigns-status.py
    docker exec cbmail-web python /tmp/campaigns-status.py

Код возврата всегда 0: это отчёт, а не проверка.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

#: Сколько последних записей журнала смотреть в поисках причины переноса.
_REASON_LOOKBACK = 5


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _ago(moment: datetime | None, now: datetime) -> str:
    moment = _as_utc(moment)
    if moment is None:
        return "неизвестно когда"
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    return f"{minutes // 60} ч {minutes % 60} мин назад"


def main() -> int:  # noqa: C901 — отчёт линейный, дробить его на части некуда
    try:
        from sqlalchemy import func, select
        from sqlalchemy.orm import Session as OrmSession

        from cbmail.db.models import Campaign, CampaignLog, SmtpProfile
        from cbmail.db.session import get_sync_engine
        from cbmail.domain.campaign import CampaignStatus
    except Exception as exc:  # noqa: BLE001 — сообщение важнее разбора причины
        print(f"Состояние рассылок получить не удалось: {exc}")
        return 0

    now = datetime.now(timezone.utc)

    with OrmSession(get_sync_engine()) as session:
        counts = dict(
            session.execute(
                select(Campaign.status, func.count()).group_by(Campaign.status)
            ).all()
        )
        if not counts:
            print("Кампаний нет.")
            return 0

        print("Кампании по статусам:")
        for status, count in sorted(counts.items()):
            try:
                label = CampaignStatus(status).label
            except ValueError:
                label = status
            print(f"  {label}: {count}")

        concurrency = os.environ.get("CELERY_CONCURRENCY", "6 (по умолчанию)")
        sending = counts.get(CampaignStatus.SENDING.value, 0)
        print(f"\nОдновременно отправляется: {sending}, мест у воркера: {concurrency}")
        if str(concurrency).split()[0].isdigit() and sending >= int(str(concurrency).split()[0]):
            print("  Все места заняты — остальные кампании ждут очереди. Это не поломка.")
            print("  Ускорить: поднять CELERY_CONCURRENCY в .env и перезапустить worker.")

        # «В очереди» — ждут свободного места. Долгое ожидание означает либо
        # занятый воркер, либо потерянную задачу (её вернёт сборщик зависших).
        queued = session.execute(
            select(Campaign.id, Campaign.name, Campaign.updated_at)
            .where(Campaign.status == CampaignStatus.QUEUED.value)
            .order_by(Campaign.updated_at)
        ).all()
        if queued:
            print(f"\nЖдут очереди ({len(queued)}):")
            for campaign_id, name, updated_at in queued:
                print(f"  #{campaign_id} {name} — в очереди с {_ago(updated_at, now)}")

        # «Отправляется» без свежей отметки активности — кандидат на зависание.
        active = session.execute(
            select(Campaign.id, Campaign.name, Campaign.updated_at, Campaign.message_interval)
            .where(Campaign.status == CampaignStatus.SENDING.value)
            .order_by(Campaign.updated_at)
        ).all()
        if active:
            print(f"\nОтправляются ({len(active)}):")
            for campaign_id, name, updated_at, interval in active:
                note = ""
                last_seen = _as_utc(updated_at)
                if last_seen is not None:
                    quiet = max(15, (interval or 0) // 60 + 10)
                    if last_seen < now - timedelta(minutes=quiet):
                        note = "  ← молчит дольше нормы, будет возвращена в расписание"
                print(f"  #{campaign_id} {name} — активность {_ago(updated_at, now)}{note}")

        # «Запланирована» на будущее — самый частый повод для вопроса «почему
        # висит». Причина переноса записана в журнале, но её никто там не ищет.
        future = session.execute(
            select(Campaign.id, Campaign.name, Campaign.scheduled_time)
            .where(
                Campaign.status == CampaignStatus.SCHEDULED.value,
                Campaign.scheduled_time.is_not(None),
                Campaign.scheduled_time > now,
            )
            .order_by(Campaign.scheduled_time)
        ).all()
        if future:
            print(f"\nЗапланированы на будущее ({len(future)}) — сегодня не уйдут:")
            for campaign_id, name, scheduled_time in future:
                when = _as_utc(scheduled_time)
                stamp = when.strftime("%d.%m.%Y %H:%M UTC") if when else "—"
                print(f"  #{campaign_id} {name} — на {stamp}")
                reason = session.execute(
                    select(CampaignLog.error_message)
                    .where(
                        CampaignLog.campaign_id == campaign_id,
                        CampaignLog.status == "deferred",
                    )
                    .order_by(CampaignLog.created_at.desc())
                    .limit(_REASON_LOOKBACK)
                ).scalars().first()
                if reason:
                    print(f"      причина: {reason}")

        # Суточная квота ящика — вторая по частоте причина «висит в
        # запланирована»: кампания стартует и тут же переносится на завтра.
        profiles = session.execute(
            select(SmtpProfile).where(SmtpProfile.daily_limit > 0).order_by(SmtpProfile.id)
        ).scalars().all()
        if profiles:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            print("\nСуточные лимиты ящиков:")
            for profile in profiles:
                used = session.execute(
                    select(func.count()).where(
                        CampaignLog.smtp_id == profile.id,
                        CampaignLog.status == "sent",
                        CampaignLog.created_at >= day_start,
                    )
                ).scalar() or 0
                left = profile.daily_limit - used
                mark = "  ← квота исчерпана, остаток уйдёт после полуночи" if left <= 0 else ""
                print(
                    f"  {profile.email_host_user}: {used} из {profile.daily_limit}, "
                    f"осталось {max(0, left)}{mark}"
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
