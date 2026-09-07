"""Отправка кампании массовой рассылки.

Самый ответственный код в системе: ошибка здесь означает либо неотправленную
рассылку, либо — гораздо хуже — отправленную дважды.

Устройство защиты от повторной отправки:

1. **Захват кампании атомарным UPDATE.** Перевод в статус «Отправляется»
   выполняется одним запросом с условием на текущий статус. Кто выиграл гонку,
   тот и отправляет; остальные получают ноль изменённых строк и выходят.
   В Django-версии проверка статуса и его установка были отдельными запросами,
   поэтому два одновременных запуска (двойной клик, повторная доставка задачи
   Celery, наложение расписания на кнопку «Отправить сейчас») проходили проверку
   оба и рассылали кампанию дважды.

2. **Лог как источник правды.** Перед отправкой из списка вычитаются адреса,
   уже помеченные как ``sent``. Даже если захват каким-то образом обойдён,
   повторного письма конкретный адресат не получит.

3. **Счётчики инкрементируются в БД.** ``sent_count = sent_count + 1`` вместо
   чтения в память, увеличения и записи целиком — иначе параллельная запись
   (например, сброс счётчиков из интерфейса) затирала прогресс.

4. **Heartbeat.** Отметка активности обновляется по ходу отправки, поэтому
   кампания с большим интервалом между письмами не выглядит зависшей и не
   сбрасывается сборщиком «зомби» в разгар работы.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from cbmail.core.config import get_settings
from cbmail.db.models import (
    Campaign,
    CampaignFile,
    CampaignLog,
    SmtpProfile,
    StopListRow,
    Unsubscribe,
)
from cbmail.domain import composer
from cbmail.domain.campaign import CampaignStatus, build_send_plan, clamp_batch_size, clamp_interval
from cbmail.domain.emails import email_domain, normalize_email, parse_emails
from cbmail.smtp.sender import (
    Attachment,
    OutgoingMessage,
    SmtpCredentials,
    SmtpError,
    SmtpSession,
    resolve_from_email,
)

logger = logging.getLogger(__name__)

# Статусы записей лога.
LOG_SENT = "sent"
LOG_FAILED = "failed"
LOG_SYSTEM = "system"

# Насколько давно кампания должна не подавать признаков жизни, чтобы считаться
# брошенной. Берётся с запасом относительно интервала между письмами.
ZOMBIE_BASE_MINUTES = 15
ZOMBIE_HEADROOM_MINUTES = 10


class CampaignNotSendable(Exception):
    """Кампанию нельзя отправлять — с человекочитаемой причиной."""


@dataclass(slots=True)
class SendResult:
    campaign_id: int
    sent: int = 0
    failed: int = 0
    skipped_stoplist: int = 0
    skipped_invalid: int = 0
    stopped_by_user: bool = False
    already_complete: bool = False
    deferred: bool = False
    """Часть аудитории перенесена на следующие сутки из-за суточного лимита ящика."""
    errors: list[str] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return self.sent + self.failed


def send_campaign(
    session: Session,
    campaign_id: int,
    *,
    force: bool = False,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SendResult:
    """Отправить кампанию. Возвращает статистику, не бросает при отказе адресата.

    ``sleep`` и ``now`` инжектируются, чтобы тесты не ждали реального времени:
    проверка пауз между письмами иначе занимала бы часы.
    """
    campaign = _acquire(session, campaign_id, force=force, now=now)
    result = SendResult(campaign_id=campaign_id)

    try:
        # SMTP разрешаем первым: домен адреса «От кого» задаёт область отписки,
        # по которой из получателей вычитаются отписавшиеся именно от этого
        # отправителя. Так план учитывает адресные отписки, а не только глобальные.
        credentials, from_email, profile = _resolve_smtp(session, campaign, now=now)
        scope_domain = email_domain(from_email)

        plan = _build_plan(session, campaign, scope_domain=scope_domain)
        result.skipped_stoplist = plan.skipped_stoplist
        result.skipped_invalid = plan.skipped_invalid

        if plan.is_complete:
            # Все адресаты получили письмо в предыдущих запусках — это успешное
            # завершение, а не ошибка «нет получателей».
            _finish(session, campaign, CampaignStatus.SENT, now=now, total=plan.total,
                    sent=plan.total)
            result.already_complete = True
            return result

        if plan.is_empty:
            _abort(session, campaign, "Нет получателей для рассылки", now=now)
            raise CampaignNotSendable("Нет получателей для рассылки")

        if dry_run:
            logger.info(
                "dry-run: кампания %s готова к отправке на %d адресов",
                campaign_id,
                len(plan.recipients),
            )
            _release(session, campaign, CampaignStatus.SCHEDULED, now=now)
            return result

        # Суточный лимит на ящик (напр. 1000/день). Считаем, сколько уже ушло с
        # этого профиля сегодня, и обрезаем план до остатка. Если лимит исчерпан —
        # переносим кампанию на начало следующих суток, не отправляя ничего.
        capped_by_limit = False
        remaining_today = _remaining_today(session, profile, now=now)
        if remaining_today is not None:
            if remaining_today <= 0:
                _defer_to_next_day(
                    session, campaign, "Суточный лимит ящика исчерпан", now=now
                )
                result.deferred = True
                return result
            if len(plan.recipients) > remaining_today:
                plan = replace(plan, recipients=plan.recipients[:remaining_today])
                capped_by_limit = True

        # Тело письма собирается один раз на кампанию; для каждого получателя
        # выполняется только подстановка адреса.
        message = _compose(campaign)
        attachments = _load_attachments(session, campaign)

        _start(session, campaign, plan.total, plan.already_sent, now=now)

        interval = clamp_interval(campaign.message_interval)
        batch_size = clamp_batch_size(campaign.message_count)

        for batch in plan.batches(batch_size):
            if _is_cancelled(session, campaign_id):
                result.stopped_by_user = True
                break
            stopped = _send_batch(
                session,
                campaign_id=campaign_id,
                recipients=batch,
                credentials=credentials,
                from_email=from_email,
                message=message,
                attachments=attachments,
                interval=interval,
                result=result,
                sleep=sleep,
                now=now,
                scope_domain=scope_domain,
            )
            if stopped:
                result.stopped_by_user = True
                break

        if result.stopped_by_user:
            _finish(session, campaign, CampaignStatus.CANCELLED, now=now)
        elif capped_by_limit:
            # Ушла только часть из-за суточного лимита — не помечаем «Отправлена»,
            # а переносим остаток на следующие сутки: уже отправленные адреса
            # отфильтруются при следующем запуске автоматически.
            _defer_to_next_day(
                session, campaign,
                "Достигнут суточный лимит ящика — остаток перенесён", now=now
            )
            result.deferred = True
        else:
            _finish(session, campaign, CampaignStatus.SENT, now=now)
        return result

    except CampaignNotSendable:
        raise
    except Exception as exc:
        # Кампания не должна навсегда остаться в статусе «Отправляется»:
        # возвращаем в черновик, чтобы её можно было перезапустить осознанно.
        logger.exception("Кампания %s упала при отправке", campaign_id)
        session.rollback()
        _abort(session, campaign_id, f"Сбой отправки: {exc}", now=now)
        raise


# --- Захват и статусы --------------------------------------------------------


def _acquire(
    session: Session, campaign_id: int, *, force: bool, now: Callable[[], datetime]
) -> Campaign:
    """Атомарно занять кампанию под отправку.

    Одним UPDATE помечаем кампанию как занятую при условии, что её ещё никто не
    занял. Гонка исчезает: между проверкой и установкой статуса нет промежутка,
    в который мог бы вклиниться второй воркер.
    """
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotSendable(f"Кампания {campaign_id} не найдена")

    if campaign.status == CampaignStatus.SENT:
        # force намеренно НЕ обходит эту проверку: кнопка «Отправить сейчас»
        # ставит задачу в очередь, и если за время ожидания расписание успело
        # отправить кампанию само, форс-запуск разослал бы всё повторно.
        raise CampaignNotSendable("Кампания уже отправлена — повторная отправка не выполняется")

    if not force and campaign.status not in (CampaignStatus.SCHEDULED, CampaignStatus.DRAFT):
        raise CampaignNotSendable(
            f"Кампанию нельзя отправить из статуса «{CampaignStatus(campaign.status).label}»"
        )

    claimed = session.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id, Campaign.status != CampaignStatus.SENDING.value)
        .values(status=CampaignStatus.SENDING.value, updated_at=now())
        .returning(Campaign.id)
    ).scalar_one_or_none()

    if claimed is None:
        raise CampaignNotSendable("Рассылка уже выполняется")

    session.commit()
    session.refresh(campaign)
    return campaign


def _start(
    session: Session,
    campaign: Campaign,
    total: int,
    already_sent: int,
    *,
    now: Callable[[], datetime],
) -> None:
    """Зафиксировать исходные счётчики перед отправкой.

    ``total`` — вся аудитория, ``sent_count`` — те, кто уже получил письмо
    ранее. Благодаря этому прогресс при возобновлении продолжается, а не
    обнуляется.
    """
    session.execute(
        update(Campaign)
        .where(Campaign.id == campaign.id)
        .values(
            total_recipients=total,
            sent_count=already_sent,
            failed_count=0,
            updated_at=now(),
        )
    )
    session.commit()


def _finish(
    session: Session,
    campaign: Campaign | int,
    status: CampaignStatus,
    *,
    now: Callable[[], datetime],
    total: int | None = None,
    sent: int | None = None,
) -> None:
    campaign_id = campaign if isinstance(campaign, int) else campaign.id
    values: dict[str, object] = {"status": status.value, "updated_at": now()}
    if status is CampaignStatus.SENT:
        values["sent_at"] = now()
    if total is not None:
        values["total_recipients"] = total
    if sent is not None:
        values["sent_count"] = sent

    session.execute(update(Campaign).where(Campaign.id == campaign_id).values(**values))
    session.commit()


def _release(
    session: Session, campaign: Campaign, status: CampaignStatus, *, now: Callable[[], datetime]
) -> None:
    session.execute(
        update(Campaign)
        .where(Campaign.id == campaign.id)
        .values(status=status.value, updated_at=now())
    )
    session.commit()


def _abort(
    session: Session, campaign: Campaign | int, reason: str, *, now: Callable[[], datetime]
) -> None:
    """Вернуть кампанию в черновик и записать причину.

    Расписание НЕ стирается: в старой версии при временной недоступности SMTP
    поле ``scheduled_time`` обнулялось, и пользователь молча терял назначенное
    им время отправки.
    """
    campaign_id = campaign if isinstance(campaign, int) else campaign.id
    session.add(
        CampaignLog(
            campaign_id=campaign_id,
            email=LOG_SYSTEM,
            status=LOG_FAILED,
            error_message=reason[:2000],
        )
    )
    session.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id)
        .values(status=CampaignStatus.DRAFT.value, updated_at=now())
    )
    session.commit()


def _is_cancelled(session: Session, campaign_id: int) -> bool:
    """Проверка кнопки «Стоп» — один дешёвый запрос по первичному ключу."""
    session.expire_all()
    status = session.execute(
        select(Campaign.status).where(Campaign.id == campaign_id)
    ).scalar_one_or_none()
    return status == CampaignStatus.CANCELLED.value


# --- Подготовка данных -------------------------------------------------------


def _build_plan(session: Session, campaign: Campaign, *, scope_domain: str = ""):
    stoplist = _load_global_stoplist(session)
    stoplist |= _load_unsubscribes(session, scope_domain)
    already_sent = _load_already_sent(session, campaign.id)
    return build_send_plan(
        raw_recipients=campaign.recipient_emails,
        global_stoplist=stoplist,
        campaign_stoplist=campaign.stop_list,
        already_sent=already_sent,
    )


def _load_global_stoplist(session: Session) -> set[str]:
    rows = session.execute(select(StopListRow.list)).scalars().all()
    result: set[str] = set()
    for raw in rows:
        result.update(normalize_email(email) for email in parse_emails(raw))
    return result


def _load_unsubscribes(session: Session, scope_domain: str) -> set[str]:
    """Адреса, отписавшиеся от писем этого отправителя (или глобально).

    Учитываются записи с пустой областью (``scope == ""`` — глобальная отписка
    из старых писем) и записи, чья область совпадает с доменом отправителя. Так
    отписка от одного домена не блокирует письма от другого.
    """
    scopes = {"", scope_domain} if scope_domain else {""}
    rows = session.execute(
        select(Unsubscribe.email).where(Unsubscribe.scope.in_(scopes))
    ).scalars()
    return {normalize_email(email) for email in rows}


def _load_already_sent(session: Session, campaign_id: int) -> set[str]:
    """Адреса, которым письмо уже ушло в предыдущих запусках."""
    rows = session.execute(
        select(CampaignLog.email).where(
            CampaignLog.campaign_id == campaign_id, CampaignLog.status == LOG_SENT
        )
    ).scalars()
    return {normalize_email(email) for email in rows}


def _resolve_smtp(
    session: Session, campaign: Campaign, *, now: Callable[[], datetime]
) -> tuple[SmtpCredentials, str, SmtpProfile]:
    profile = campaign.smtp
    if profile is None:
        profile = session.execute(
            select(SmtpProfile)
            .where(SmtpProfile.user_id == campaign.user_id)
            .order_by(SmtpProfile.id)
            .limit(1)
        ).scalar_one_or_none()

    if profile is None:
        _abort(session, campaign, "Не найдены настройки SMTP", now=now)
        raise CampaignNotSendable(
            "Не настроены параметры почты — откройте «Почта → Настройки»"
        )

    try:
        credentials = SmtpCredentials.from_profile(profile)
    except SmtpError as exc:
        _abort(session, campaign, str(exc), now=now)
        raise CampaignNotSendable(str(exc)) from exc

    from_email = resolve_from_email(credentials, campaign.from_email or profile.default_from_email)
    return credentials, from_email, profile


# --- Суточный лимит на ящик --------------------------------------------------


def _day_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """Границы календарных суток (UTC), в которые попадает ``moment``."""
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _sent_today(session: Session, profile_id: int, *, now: Callable[[], datetime]) -> int:
    """Сколько писем ушло с этого SMTP-профиля за текущие сутки (по логам)."""
    start, end = _day_bounds(now())
    return int(
        session.execute(
            select(func.count())
            .select_from(CampaignLog)
            .join(Campaign, Campaign.id == CampaignLog.campaign_id)
            .where(
                CampaignLog.status == LOG_SENT,
                Campaign.use_smtp_settings_id == profile_id,
                CampaignLog.created_at >= start,
                CampaignLog.created_at < end,
            )
        ).scalar_one()
    )


def _remaining_today(
    session: Session, profile: SmtpProfile, *, now: Callable[[], datetime]
) -> int | None:
    """Остаток суточного лимита ящика. ``None`` — лимит не задан (безлимит)."""
    limit = getattr(profile, "daily_limit", 0) or 0
    if limit <= 0:
        return None
    return max(0, limit - _sent_today(session, profile.id, now=now))


def _next_day_start(moment: datetime) -> datetime:
    """Начало следующих суток (00:05 UTC) — когда суточный счётчик обнулится."""
    start, _ = _day_bounds(moment)
    return start + timedelta(days=1, minutes=5)


def _defer_to_next_day(
    session: Session, campaign: Campaign | int, reason: str, *, now: Callable[[], datetime]
) -> None:
    """Перенести кампанию на начало следующих суток (для суточного лимита).

    Ставим статус «Запланирована» и время на 00:05 следующего дня — тогда
    планировщик подхватит кампанию, когда лимит ящика обнулится, и дошлёт
    остаток (уже отправленные адреса отфильтруются автоматически).
    """
    campaign_id = campaign if isinstance(campaign, int) else campaign.id
    next_run = _next_day_start(now())
    session.add(
        CampaignLog(
            campaign_id=campaign_id,
            email=LOG_SYSTEM,
            status="deferred",
            error_message=(f"{reason}. Перенесено на {next_run:%d.%m.%Y %H:%M} UTC.")[:2000],
        )
    )
    session.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id)
        .values(
            status=CampaignStatus.SCHEDULED.value,
            scheduled_time=next_run,
            updated_at=now(),
        )
    )
    session.commit()


def _compose(campaign: Campaign) -> composer.ComposedMessage:
    profile = campaign.smtp
    # Ссылка отписки добавляется автоматически, если пользователь не вставил её сам —
    # обычному пользователю не нужно знать про плейсхолдеры, а массовое письмо без
    # отписки портит репутацию домена. Для письма из простого текста собирается и
    # HTML-версия, чтобы «Отписаться» была кликабельным словом.
    return composer.compose(
        subject=campaign.subject,
        body=campaign.message,
        header=profile.message_header if profile else None,
        footer=profile.message_footer if profile else None,
        add_unsubscribe=True,
    )


def _load_attachments(session: Session, campaign: Campaign) -> list[Attachment]:
    """Прочитать вложения один раз на всю кампанию.

    Раньше файлы читались с диска при формировании каждого письма — на рассылке
    в тысячи адресов это тысячи лишних чтений одного и того же файла.
    """
    settings = get_settings()
    rows = session.execute(
        select(CampaignFile).where(CampaignFile.campaign_id == campaign.id)
    ).scalars().all()

    attachments: list[Attachment] = []
    for row in rows:
        path = Path(settings.media_root) / row.file
        try:
            content = path.read_bytes()
        except OSError as exc:
            # Отсутствие вложения — повод предупредить, но не отменять рассылку.
            logger.warning("Вложение %s недоступно: %s", path, exc)
            continue
        if len(content) > settings.smtp_attachment_max_bytes:
            logger.warning("Вложение %s больше лимита — пропущено", row.filename)
            continue
        attachments.append(
            Attachment(filename=row.filename, content=content, mime_type=_guess_mime(row.filename))
        )
    return attachments


def _guess_mime(filename: str) -> str:
    import mimetypes

    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


# --- Собственно отправка -----------------------------------------------------


def _send_batch(
    session: Session,
    *,
    campaign_id: int,
    recipients: tuple[str, ...],
    credentials: SmtpCredentials,
    from_email: str,
    message: composer.ComposedMessage,
    attachments: list[Attachment],
    interval: int,
    result: SendResult,
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
    scope_domain: str = "",
) -> bool:
    """Отправить пачку в одном соединении. Возвращает True, если пользователь нажал «Стоп»."""
    settings = get_settings()
    base_url = _base_url()

    try:
        with SmtpSession(credentials) as smtp:
            for index, email in enumerate(recipients):
                if _is_cancelled(session, campaign_id):
                    return True

                unsub_url = composer.build_unsubscribe_url(base_url, email, scope=scope_domain)
                personalized = message.personalize(email, unsub_url)
                outgoing = OutgoingMessage(
                    subject=personalized.subject,
                    text_body=personalized.text_body,
                    html_body=personalized.html_body,
                    from_email=from_email,
                    attachments=attachments,
                    headers={
                        # Позволяет почтовым клиентам показать штатную кнопку
                        # «Отписаться» — без неё пользователи жмут «Спам»,
                        # что портит репутацию домена отправителя.
                        "List-Unsubscribe": f"<{unsub_url}>",
                    },
                )

                try:
                    smtp.send(outgoing, email)
                except SmtpError as exc:
                    _log_failure(session, campaign_id, email, exc, now=now)
                    result.failed += 1
                    result.errors.append(f"{email}: {exc}")
                    if not exc.permanent and isinstance(exc, SmtpError) and _is_fatal(exc):
                        # Соединение разорвано — остаток пачки уйдёт в следующем
                        # заходе; продолжать в мёртвом сокете бессмысленно.
                        raise
                else:
                    _log_success(session, campaign_id, email, now=now)
                    result.sent += 1

                if interval > 0 and index < len(recipients) - 1:
                    sleep(interval)
                    # Пауза между письмами не должна выглядеть как зависание.
                    _heartbeat(session, campaign_id, now=now)

    except SmtpError as exc:
        # Ошибка уровня соединения: остальные адреса пачки не считаем
        # неудачными — они просто не были обработаны и уйдут при следующем
        # запуске. Раньше весь батч разом помечался как failed, и повторить
        # отправку по ним было уже нельзя.
        logger.warning("Пачка кампании %s прервана: %s", campaign_id, exc)
        result.errors.append(str(exc))
        session.commit()

    _ = settings
    return False


def _is_fatal(exc: SmtpError) -> bool:
    from cbmail.smtp.sender import SmtpConnectionError

    return isinstance(exc, SmtpConnectionError)


def _log_success(
    session: Session, campaign_id: int, email: str, *, now: Callable[[], datetime]
) -> None:
    session.add(CampaignLog(campaign_id=campaign_id, email=email, status=LOG_SENT))
    session.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id)
        .values(sent_count=Campaign.sent_count + 1, updated_at=now())
    )
    # Коммит на каждое письмо: при падении процесса уже отправленные письма
    # обязаны остаться зафиксированными, иначе при перезапуске они уйдут повторно.
    session.commit()


def _log_failure(
    session: Session, campaign_id: int, email: str, exc: Exception, *, now: Callable[[], datetime]
) -> None:
    session.add(
        CampaignLog(
            campaign_id=campaign_id,
            email=email,
            status=LOG_FAILED,
            error_message=str(exc)[:2000],
        )
    )
    session.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id)
        .values(failed_count=Campaign.failed_count + 1, updated_at=now())
    )
    session.commit()


def _heartbeat(session: Session, campaign_id: int, *, now: Callable[[], datetime]) -> None:
    session.execute(
        update(Campaign).where(Campaign.id == campaign_id).values(updated_at=now())
    )
    session.commit()


def _base_url() -> str:
    import os

    return os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or "http://localhost:8000"


# --- Обслуживание ------------------------------------------------------------


def find_due_campaigns(session: Session, now: datetime) -> list[int]:
    """Кампании, которым пора уходить.

    Условие строгое: статус «Запланирована» И время задано И оно наступило.
    Отсутствие времени никогда не означает «отправить сейчас».
    """
    from cbmail.domain.campaign import SCHEDULE_TOLERANCE_SECONDS

    threshold = now + timedelta(seconds=SCHEDULE_TOLERANCE_SECONDS)
    rows = session.execute(
        select(Campaign.id).where(
            Campaign.status == CampaignStatus.SCHEDULED.value,
            Campaign.scheduled_time.is_not(None),
            Campaign.scheduled_time <= threshold,
        )
    ).scalars()
    return list(rows)


def recover_stuck_campaigns(session: Session, now: datetime) -> list[int]:
    """Вернуть в черновик кампании, брошенные упавшим воркером.

    Порог зависит от настроенного интервала между письмами: кампания с паузой
    в десять минут не подаёт признаков жизни дольше, чем кампания без пауз, и
    не должна из-за этого считаться зависшей. Раньше фиксированный порог
    сбрасывал в черновик активно работающие рассылки прямо посреди отправки.
    """
    longest_interval = (
        session.execute(
            select(func.max(Campaign.message_interval)).where(
                Campaign.status == CampaignStatus.SENDING.value
            )
        ).scalar()
        or 0
    )
    headroom = max(
        ZOMBIE_BASE_MINUTES, longest_interval // 60 + ZOMBIE_HEADROOM_MINUTES
    )
    threshold = now - timedelta(minutes=headroom)

    stuck = session.execute(
        select(Campaign.id).where(
            Campaign.status == CampaignStatus.SENDING.value, Campaign.updated_at < threshold
        )
    ).scalars().all()

    for campaign_id in stuck:
        logger.warning("Кампания %s зависла в отправке — сбрасываю в черновик", campaign_id)
        session.add(
            CampaignLog(
                campaign_id=campaign_id,
                email=LOG_SYSTEM,
                status=LOG_FAILED,
                error_message=f"Брошена в статусе «Отправляется» дольше {headroom} мин — сброшена",
            )
        )
        session.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(status=CampaignStatus.DRAFT.value, updated_at=now)
        )
    session.commit()
    return list(stuck)


def iter_recipients_preview(campaign: Campaign, limit: int = 50) -> Iterator[str]:
    """Первые адреса кампании — для интерфейса, без загрузки всего списка."""
    for index, email in enumerate(parse_emails(campaign.recipient_emails)):
        if index >= limit:
            return
        yield email
