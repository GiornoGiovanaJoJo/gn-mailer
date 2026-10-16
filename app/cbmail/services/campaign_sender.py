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

import contextlib
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_ as sa_and
from sqlalchemy import func, select, update
from sqlalchemy import or_ as sa_or
from sqlalchemy.orm import Session, sessionmaker

from cbmail.core.config import get_settings
from cbmail.db.models import (
    Campaign,
    CampaignFile,
    CampaignLog,
    SmtpProfile,
    StopListRow,
    Unsubscribe,
)
from cbmail.domain import composer, tracking
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

# Сколько кампания может простоять «В очереди», прежде чем считать задачу
# потерянной и вернуть кампанию в расписание. С запасом относительно минутного
# тика: очередь бывает длинной, и ждать своего места — это нормально.
QUEUE_STALE_MINUTES = 20

# Сколько раз за один заход переоткрывать оборвавшееся SMTP-соединение и
# продолжать с оставшихся адресов. Нужно, потому что некоторые провайдеры
# (в частности mail.ru) периодически рвут соединение на очередном письме —
# без переподключения все адреса ПОСЛЕ оборвавшегося молча пропускались, а
# кампания помечалась «Отправлена», и эти люди не получали письмо никогда.
MAX_RECONNECTS_PER_BATCH = 5

# Через сколько повторить кампанию, если за заход обработать всех не удалось
# (соединение рвалось чаще, чем позволяет бюджет переподключений).
RETRY_DELAY_MINUTES = 10

# Сколько ящиков одной рассылки работают одновременно.
#
# Каждый держит своё соединение с базой и своё — с почтовым сервером, а пул
# конечен и кампаний одновременно идёт тоже несколько. Три ящика дают втрое
# более быструю рассылку и предсказуемую нагрузку; при необходимости число
# поднимается здесь, вместе с размером пула подключений.
MAX_PARALLEL_MAILBOXES = 3


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
    interrupted: bool = False
    """Соединение рвалось чаще бюджета переподключений — остаток не обработан, нужен повтор."""
    shutting_down: bool = False
    """Воркер останавливают (выкатка) — остаток дошлём после перезапуска."""
    errors: list[str] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return self.sent + self.failed


# Воркер получил SIGTERM — обычно это выкатка новой версии.
#
# Раньше рассылку в этот момент просто убивали: кампания оставалась в статусе
# «Отправляется» с половиной разосланных писем, и вернуть её в работу мог
# только сборщик зависших — через четверть часа. Теперь отправка замечает
# остановку на ближайшей границе между письмами и сама просит повтор.
_SHUTDOWN = threading.Event()


def request_shutdown() -> None:
    """Попросить идущие рассылки завершиться на ближайшей границе письма."""
    _SHUTDOWN.set()


def cancel_shutdown() -> None:
    """Сбросить признак остановки (нужно тестам и перезапуску в одном процессе)."""
    _SHUTDOWN.clear()


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
        # Ящики разрешаем первыми: домен адреса «От кого» задаёт область
        # отписки, по которой из получателей вычитаются отписавшиеся именно от
        # этого отправителя. Так план учитывает адресные отписки, а не только
        # глобальные.
        #
        # Ящиков может быть несколько, и домены у них разные. Тогда общий план
        # строится только по глобальным отпискам, а свой домен каждый ящик
        # отсеивает сам — вычесть объединение значило бы не отправить письмо
        # тем, кто отписался лишь от одного из отправителей.
        senders = _resolve_senders(session, campaign, now=now)
        scope_domain = senders[0].scope_domain if len(senders) == 1 else ""

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
        # каждого профиля сегодня, и обрезаем план до суммы остатков. Если
        # квота исчерпана у всех — переносим кампанию на начало следующих
        # суток, не отправляя ничего.
        capped_by_limit = False
        live = [s for s in senders if s.remaining_today is None or s.remaining_today > 0]
        if not live:
            _defer_to_next_day(
                session,
                campaign,
                "Суточный лимит ящика исчерпан"
                if len(senders) == 1
                else f"Суточный лимит исчерпан у всех ящиков рассылки ({len(senders)})",
                now=now,
            )
            result.deferred = True
            return result
        # Безлимитный ящик снимает ограничение со всей рассылки: остаток, который
        # не возьмут остальные, уйдёт через него.
        capacity = (
            None
            if any(s.remaining_today is None for s in live)
            else sum(s.remaining_today or 0 for s in live)
        )
        if capacity is not None and len(plan.recipients) > capacity:
            plan = replace(plan, recipients=plan.recipients[:capacity])
            capped_by_limit = True

        # Тело письма собирается один раз на кампанию; для каждого получателя
        # выполняется только подстановка адреса.
        message = _compose(campaign)
        attachments = _load_attachments(session, campaign)

        _start(session, campaign, plan.total, plan.already_sent, now=now)

        interval = clamp_interval(campaign.message_interval)
        batch_size = clamp_batch_size(campaign.message_count)

        # Общая очередь адресов на все ящики: кто освободился, тот и берёт
        # следующий. Порядок тот же, что в плане — батчи здесь не нужны, пауза
        # между письмами соблюдается каждым ящиком отдельно.
        pending: queue.Queue[str] = queue.Queue()
        for email in plan.recipients:
            pending.put(email)

        _merge(
            result,
            _dispatch(
                live,
                session=session,
                campaign_id=campaign_id,
                run_token=campaign.run_token,
                pending=pending,
                message=message,
                attachments=attachments,
                interval=interval,
                batch_size=batch_size,
                sleep=sleep,
                now=now,
            ),
        )
        # Остаток в очереди означает, что ящики выбыли раньше списка: кончились
        # квоты либо рвалось соединение. Кампания не должна считаться
        # отправленной — остаток дошлём следующим заходом.
        if not pending.empty() and not result.stopped_by_user and not result.interrupted:
            capped_by_limit = True

        if result.shutting_down:
            # Выкатка новой версии. Остаток дошлём, как только воркер поднимется:
            # возвращаем кампанию в расписание сами, не дожидаясь сборщика
            # зависших — иначе рассылка стояла бы ещё четверть часа.
            _requeue_for_retry(
                session, campaign, "Сервис перезапускался — остаток будет дослан", now=now
            )
            result.deferred = True
        elif result.stopped_by_user:
            _finish(session, campaign, CampaignStatus.CANCELLED, now=now)
        elif result.interrupted:
            # Соединение рвалось чаще бюджета переподключений — часть адресов не
            # обработана. НЕ помечаем «Отправлена» (иначе они не уйдут никогда).
            if result.sent > 0:
                # Прогресс есть — скорее всего временный сбой у провайдера,
                # ставим на повтор; уже отправленные адреса при повторе отсеются.
                _requeue_for_retry(
                    session, campaign,
                    "SMTP-соединение прерывалось — остаток будет дослан", now=now
                )
                result.deferred = True
            else:
                # Не ушло ни одного письма — вероятно, проблема с настройками
                # почты. Возвращаем в черновик, чтобы не зациклиться на повторах.
                _abort(
                    session, campaign,
                    "SMTP-соединение постоянно прерывается — проверьте настройки почты",
                    now=now,
                )
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
        _abort(session, campaign, f"Сбой отправки: {exc}", now=now)
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

    if not force and campaign.status not in (
        CampaignStatus.SCHEDULED,
        CampaignStatus.QUEUED,
        CampaignStatus.DRAFT,
    ):
        raise CampaignNotSendable(
            f"Кампанию нельзя отправить из статуса «{CampaignStatus(campaign.status).label}»"
        )

    # Метка запуска: по ней последующие записи статуса отличают «эту отправку»
    # от задачи-двойника, пережившей перезапуск воркера.
    token = uuid.uuid4().hex
    claimed = session.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id, Campaign.status != CampaignStatus.SENDING.value)
        .values(status=CampaignStatus.SENDING.value, run_token=token, updated_at=now())
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


def _write_status(
    session: Session,
    campaign: Campaign,
    values: dict[str, object],
    *,
    now: Callable[[], datetime],
) -> bool:
    """Записать итог отправки — но только от имени текущего запуска.

    Условие на метку запуска здесь важнее, чем кажется. Пауза между письмами
    может длиться минуты, и за это время кампанию успевают остановить и
    запустить заново. Без проверки старый поток, проснувшись, дописывал свой
    итог поверх: нажатый «Стоп» превращался обратно в «Отправлена», а заново
    поставленная в очередь кампания — в «Черновик», которого расписание уже не
    видит. Снаружи это выглядело как «кампания больше никогда не запускается».

    Ноль затронутых строк означает, что отправку перехватил кто-то другой —
    это штатный исход, а не сбой: новый запуск сам доведёт кампанию до конца.
    """
    token = campaign.run_token
    result = session.execute(
        update(Campaign)
        .where(
            Campaign.id == campaign.id,
            Campaign.status == CampaignStatus.SENDING.value,
            Campaign.run_token == token,
        )
        .values(**values, updated_at=now())
    )
    session.commit()
    if not result.rowcount:
        logger.info(
            "Кампания %s: итог запуска %s не записан — отправку уже ведёт другой запуск",
            campaign.id,
            token,
        )
        return False
    return True


def _finish(
    session: Session,
    campaign: Campaign,
    status: CampaignStatus,
    *,
    now: Callable[[], datetime],
    total: int | None = None,
    sent: int | None = None,
) -> None:
    values: dict[str, object] = {"status": status.value, "run_token": None}
    if status is CampaignStatus.SENT:
        values["sent_at"] = now()
    if total is not None:
        values["total_recipients"] = total
    if sent is not None:
        values["sent_count"] = sent

    _write_status(session, campaign, values, now=now)


def _release(
    session: Session, campaign: Campaign, status: CampaignStatus, *, now: Callable[[], datetime]
) -> None:
    _write_status(session, campaign, {"status": status.value, "run_token": None}, now=now)


def _abort(
    session: Session, campaign: Campaign, reason: str, *, now: Callable[[], datetime]
) -> None:
    """Вернуть кампанию в черновик и записать причину.

    Расписание НЕ стирается: в старой версии при временной недоступности SMTP
    поле ``scheduled_time`` обнулялось, и пользователь молча терял назначенное
    им время отправки.

    Запись в журнал идёт всегда, даже если статус писать уже не наше дело:
    ошибка случилась, и след о ней нужен тому, кто будет разбираться.
    """
    session.add(
        CampaignLog(
            campaign_id=campaign.id,
            email=LOG_SYSTEM,
            status=LOG_FAILED,
            error_message=reason[:2000],
        )
    )
    _write_status(
        session,
        campaign,
        {"status": CampaignStatus.DRAFT.value, "run_token": None},
        now=now,
    )


def _must_stop(session: Session, campaign_id: int, run_token: str | None) -> bool:
    """Пора ли прекращать отправку — один дешёвый запрос по первичному ключу.

    Проверяем не только нажатый «Стоп», но и то, что рассылку всё ещё ведём мы.
    Между письмами проходят минуты, и за это время кампанию успевают остановить
    и запустить заново. Раньше проверка смотрела только на статус «Отменена»:
    перезапущенная кампания получала статус «Запланирована», старый поток видел
    «не отменена» и продолжал слать — то есть по одному списку шли два потока,
    а человек получал дубли писем.
    """
    session.expire_all()
    row = session.execute(
        select(Campaign.status, Campaign.run_token).where(Campaign.id == campaign_id)
    ).one_or_none()
    if row is None:
        return True
    status, token = row
    return status != CampaignStatus.SENDING.value or token != run_token


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
    session: Session,
    campaign: Campaign,
    profile: SmtpProfile | None = None,
    *,
    now: Callable[[], datetime],
) -> tuple[SmtpCredentials, str, SmtpProfile]:
    """Параметры отправки одним ящиком.

    ``profile`` задан — используем его; иначе прежний порядок: ящик кампании,
    а при его отсутствии первый ящик пользователя.
    """
    if profile is None:
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
    """Сколько писем ушло с этого ящика за текущие сутки (по логам).

    Ящик берём из самой записи лога. Через кампанию его определять больше
    нельзя: рассылка идёт с нескольких ящиков сразу, и по кампании не понять,
    чью квоту израсходовало конкретное письмо.

    У записей, сделанных раньше, колонка пуста — для них ящик по-прежнему
    определяется через кампанию. Иначе в день перехода суточные счётчики
    обнулились бы и лимит провайдера был бы превышен.
    """
    start, end = _day_bounds(now())
    return int(
        session.execute(
            select(func.count())
            .select_from(CampaignLog)
            .join(Campaign, Campaign.id == CampaignLog.campaign_id)
            .where(
                CampaignLog.status == LOG_SENT,
                sa_or(
                    CampaignLog.smtp_id == profile_id,
                    sa_and(
                        CampaignLog.smtp_id.is_(None),
                        Campaign.use_smtp_settings_id == profile_id,
                    ),
                ),
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
    session: Session, campaign: Campaign, reason: str, *, now: Callable[[], datetime]
) -> None:
    """Перенести кампанию на начало следующих суток (для суточного лимита).

    Ставим статус «Запланирована» и время на 00:05 следующего дня — тогда
    планировщик подхватит кампанию, когда лимит ящика обнулится, и дошлёт
    остаток (уже отправленные адреса отфильтруются автоматически).
    """
    next_run = _next_day_start(now())
    session.add(
        CampaignLog(
            campaign_id=campaign.id,
            email=LOG_SYSTEM,
            status="deferred",
            error_message=(f"{reason}. Перенесено на {next_run:%d.%m.%Y %H:%M} UTC.")[:2000],
        )
    )
    _write_status(
        session,
        campaign,
        {
            "status": CampaignStatus.SCHEDULED.value,
            "scheduled_time": next_run,
            "run_token": None,
        },
        now=now,
    )


def _requeue_for_retry(
    session: Session, campaign: Campaign, reason: str, *, now: Callable[[], datetime]
) -> None:
    """Поставить кампанию на скорый повтор (обрыв соединения не по вине адресов).

    Статус «Запланирована», время — через ``RETRY_DELAY_MINUTES`` минут: планировщик
    подхватит её и дошлёт необработанных получателей (уже отправленные отсеются).
    """
    next_run = now() + timedelta(minutes=RETRY_DELAY_MINUTES)
    session.add(
        CampaignLog(
            campaign_id=campaign.id,
            email=LOG_SYSTEM,
            status="deferred",
            error_message=(f"{reason}. Повтор в {next_run:%d.%m.%Y %H:%M} UTC.")[:2000],
        )
    )
    _write_status(
        session,
        campaign,
        {
            "status": CampaignStatus.SCHEDULED.value,
            "scheduled_time": next_run,
            "run_token": None,
        },
        now=now,
    )


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


@dataclass(slots=True)
class _Sender:
    """Один ящик на время рассылки: чем отправлять и сколько ему осталось.

    Лимиты и отказы принадлежат ящику, а не рассылке. У одного кончается
    суточная квота, у другого разваливается авторизация — остальные обязаны
    продолжать. Поэтому всё, что относится к ящику, живёт здесь, а не в общих
    переменных отправки.
    """

    profile: SmtpProfile
    credentials: SmtpCredentials
    from_email: str
    scope_domain: str
    #: Остаток суточной квоты; None — лимит не задан.
    remaining_today: int | None
    #: Адреса, отписавшиеся именно от этого отправителя. Пусто, когда ящик один:
    #: тогда отписки уже вычтены из общего плана.
    unsubscribed: frozenset[str] = frozenset()


def _resolve_senders(
    session: Session, campaign: Campaign, *, now: Callable[[], datetime]
) -> list[_Sender]:
    """Какими ящиками отправлять кампанию.

    Выбранный набор — это ``CampaignSmtp``. Пусто означает прежнее поведение:
    один ящик из ``use_smtp_settings_id`` либо первый ящик пользователя. Так
    продолжают работать все созданные раньше рассылки, и трогать их не нужно.

    Ненастроенный ящик пропускаем, а не валим всю рассылку: остальные должны
    отправить то, что им досталось. Если не осталось ни одного — это уже отказ
    с внятной причиной.
    """
    chosen = [link.smtp for link in campaign.smtp_profiles if link.smtp is not None]
    if not chosen:
        return [_build_sender(session, campaign, None, now=now)]

    if len(chosen) > MAX_PARALLEL_MAILBOXES:
        # Молча отбросить лишние нельзя: человек отметил ящики и ждёт, что они
        # работают. Говорим вслух и берём первые — каждый поток держит своё
        # соединение с базой, и пул конечен.
        logger.warning(
            "Кампания %s: выбрано ящиков %d, одновременно работают %d — остальные не участвуют",
            campaign.id,
            len(chosen),
            MAX_PARALLEL_MAILBOXES,
        )
        chosen = chosen[:MAX_PARALLEL_MAILBOXES]

    senders: list[_Sender] = []
    broken: list[str] = []
    for profile in chosen:
        try:
            senders.append(_build_sender(session, campaign, profile, now=now))
        except CampaignNotSendable as exc:
            broken.append(f"{profile.email_host_user or profile.title}: {exc}")

    if not senders:
        reason = "; ".join(broken) or "Не найдены настройки SMTP"
        _abort(session, campaign, reason, now=now)
        raise CampaignNotSendable(reason)

    if broken:
        logger.warning("Кампания %s: ящики пропущены — %s", campaign.id, "; ".join(broken))

    # Отписка адресная: отписавшийся от писем одного отправителя продолжает
    # получать от другого. Поэтому домен своего отправителя каждый ящик
    # отсеивает сам — в общий план входят только глобальные отписки.
    by_domain: dict[str, frozenset[str]] = {}
    for sender in senders:
        if sender.scope_domain not in by_domain:
            by_domain[sender.scope_domain] = frozenset(
                _load_unsubscribes(session, sender.scope_domain)
            )
        sender.unsubscribed = by_domain[sender.scope_domain]
    return senders


def _build_sender(
    session: Session,
    campaign: Campaign,
    profile: SmtpProfile | None,
    *,
    now: Callable[[], datetime],
) -> _Sender:
    """Собрать параметры отправки по одному ящику."""
    credentials, from_email, resolved = _resolve_smtp(session, campaign, profile, now=now)
    return _Sender(
        profile=resolved,
        credentials=credentials,
        from_email=from_email,
        scope_domain=email_domain(from_email),
        remaining_today=_remaining_today(session, resolved, now=now),
    )


def _run_sender(
    sender: _Sender,
    *,
    session: Session,
    campaign_id: int,
    run_token: str | None,
    pending: queue.Queue[str],
    cancelled: threading.Event,
    message: composer.ComposedMessage,
    attachments: list[Attachment],
    interval: int,
    batch_size: int,
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> SendResult:
    """Отправка одним ящиком: берёт адреса из общей очереди, пока они есть.

    Очередь общая на все ящики, и разбирают её по мере готовности, а не делят
    заранее поровну: ящики работают с разной скоростью, у одного кончается
    квота, другой может отвалиться совсем — заранее нарезанная доля в этих
    случаях осталась бы неотправленной.

    При обрыве соединения (частый случай с mail.ru: «Connection unexpectedly
    closed») соединение переоткрывается, и отправка ПРОДОЛЖАЕТСЯ со следующего
    адреса — оборвавшийся помечается неудачным, но идущие за ним не теряются.
    """
    result = SendResult(campaign_id=campaign_id)
    base_url = _base_url()
    smtp = None  # текущая открытая SMTP-сессия (или None)
    remaining = sender.remaining_today
    # Сколько писем ушло в текущем соединении. Провайдеры ограничивают их число
    # на одну сессию, поэтому соединение переоткрывается раз в пачку — раньше
    # то же давало разбиение плана на батчи.
    in_session = 0

    def _drop() -> None:
        # Закрыть соединение, проглотив любые ошибки закрытия мёртвого сокета.
        nonlocal smtp
        if smtp is not None:
            with contextlib.suppress(Exception):
                smtp.__exit__(None, None, None)
            smtp = None

    def _ensure_open() -> bool:
        # (Пере)открыть соединение с ограниченным числом попыток. False — если
        # сервер недоступен (тогда остаток кампании уйдёт отдельным заходом).
        nonlocal smtp
        if smtp is not None:
            return True
        for attempt in range(MAX_RECONNECTS_PER_BATCH + 1):
            try:
                cm = SmtpSession(sender.credentials)
                cm.__enter__()
                smtp = cm
                return True
            except SmtpError as exc:
                logger.warning(
                    "Кампания %s, ящик %s: не удалось открыть SMTP (%s), попытка %d/%d",
                    campaign_id, sender.from_email, exc, attempt + 1, MAX_RECONNECTS_PER_BATCH + 1,
                )
                result.errors.append(str(exc))
                if attempt < MAX_RECONNECTS_PER_BATCH:
                    sleep(min(5, attempt + 1))
        return False

    try:
        while True:
            if cancelled.is_set():
                result.stopped_by_user = True
                return result
            if remaining is not None and remaining <= 0:
                # Квота ящика исчерпана: остаток разберут другие, а если их нет —
                # кампания перенесётся на следующие сутки.
                result.deferred = True
                return result
            try:
                email = pending.get_nowait()
            except queue.Empty:
                return result

            if email in sender.unsubscribed:
                # Отписан именно от этого отправителя. Не возвращаем в очередь:
                # письмо «от другого ящика той же компании» — не то, на что
                # человек соглашался, отписываясь.
                result.skipped_stoplist += 1
                continue

            if _SHUTDOWN.is_set():
                # Воркер останавливают. Выходим на границе между письмами, не
                # начиная новое: письмо, оборванное на полпути, пришлось бы
                # считать неотправленным, хотя адресат его, возможно, получил.
                cancelled.set()
                result.shutting_down = True
                return result

            if _must_stop(session, campaign_id, run_token):
                cancelled.set()
                result.stopped_by_user = True
                return result

            # Интервал соблюдается ПЕРЕД каждым письмом, кроме первого за заход.
            # На время паузы соединение ЗАКРЫВАЕМ: почтовые серверы (Яндекс и др.)
            # рвут простаивающие сессии, и следующая отправка по «протухшему»
            # сокету падала бы «Connection unexpectedly closed». Перед письмом
            # открываем свежее соединение — протухать нечему.
            if batch_size > 0 and in_session >= batch_size:
                _drop()
                in_session = 0

            if interval > 0 and result.total_processed > 0:
                _drop()
                sleep(interval)
                _heartbeat(session, campaign_id, now=now)
                if _SHUTDOWN.is_set():
                    cancelled.set()
                    result.shutting_down = True
                    return result
                if _must_stop(session, campaign_id, run_token):
                    cancelled.set()
                    result.stopped_by_user = True
                    return result

            if not _ensure_open():
                # Сервер недоступен — необработанные адреса дошлём позже.
                # Взятый из очереди адрес возвращаем: его разберёт другой ящик.
                pending.put(email)
                result.interrupted = True
                return result

            unsub_url = composer.build_unsubscribe_url(base_url, email, scope=sender.scope_domain)
            personalized = message.personalize(email, unsub_url)
            # Пиксель открытия и переходы по ссылкам. Встраиваются после
            # подстановок: иначе адрес получателя в ссылке отписки не попал бы
            # в письмо, а счётчики считали бы одно письмо на всех.
            html_body = tracking.inject(
                personalized.html_body,
                base_url=base_url,
                secret=get_settings().secret_key,
                campaign_id=campaign_id,
                email=email,
                unsubscribe_url=unsub_url,
            )
            outgoing = OutgoingMessage(
                subject=personalized.subject,
                text_body=personalized.text_body,
                html_body=html_body,
                from_email=sender.from_email,
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
                _log_failure(session, campaign_id, email, exc, now=now, smtp_id=sender.profile.id)
                result.failed += 1
                result.errors.append(f"{email}: {exc}")
                # Соединение могло умереть на этом письме — сбрасываем, чтобы
                # следующий адрес получил свежее соединение (а не тот же обрыв).
                if _is_fatal(exc):
                    _drop()
            else:
                _log_success(session, campaign_id, email, now=now, smtp_id=sender.profile.id)
                result.sent += 1
                if remaining is not None:
                    remaining -= 1
            in_session += 1
    finally:
        _drop()


def _dispatch(
    senders: list[_Sender],
    *,
    session: Session,
    campaign_id: int,
    run_token: str | None,
    pending: queue.Queue[str],
    message: composer.ComposedMessage,
    attachments: list[Attachment],
    interval: int,
    batch_size: int,
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> list[SendResult]:
    """Запустить ящики. Один — в текущем потоке, несколько — параллельно.

    Один ящик намеренно обходится без потоков и без второй сессии: это прежний
    путь целиком, и он не должен меняться из-за появления многопоточности.

    Каждому потоку — своя сессия БД. Сессия SQLAlchemy не рассчитана на работу
    из нескольких потоков: общая давала бы повреждение состояния, которое
    проявляется не отказом, а неверными данными.
    """
    cancelled = threading.Event()
    if len(senders) == 1:
        return [
            _run_sender(
                senders[0],
                session=session,
                campaign_id=campaign_id,
                run_token=run_token,
                pending=pending,
                cancelled=cancelled,
                message=message,
                attachments=attachments,
                interval=interval,
                batch_size=batch_size,
                sleep=sleep,
                now=now,
            )
        ]

    # Фабрику берём от движка вызывающей сессии, а не из глобальной: так потоки
    # гарантированно работают с той же базой и тем же пулом, что и кампания —
    # глобальная завела бы второе подключение мимо вызывающего кода.
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False, autoflush=False)

    def _work(sender: _Sender) -> SendResult:
        with factory() as own:
            try:
                return _run_sender(
                    sender,
                    session=own,
                    campaign_id=campaign_id,
                    run_token=run_token,
                    pending=pending,
                    cancelled=cancelled,
                    message=message,
                    attachments=attachments,
                    interval=interval,
                    batch_size=batch_size,
                    sleep=sleep,
                    now=now,
                )
            except Exception as exc:
                own.rollback()
                logger.exception("Кампания %s, ящик %s: поток упал", campaign_id, sender.from_email)
                failed = SendResult(campaign_id=campaign_id)
                failed.interrupted = True
                failed.errors.append(f"{sender.from_email}: {exc}")
                return failed

    with ThreadPoolExecutor(max_workers=len(senders)) as pool:
        return list(pool.map(_work, senders))


def _merge(result: SendResult, parts: list[SendResult]) -> None:
    """Свести итоги ящиков в общий результат кампании."""
    for part in parts:
        result.sent += part.sent
        result.failed += part.failed
        result.skipped_stoplist += part.skipped_stoplist
        result.errors.extend(part.errors)
        result.stopped_by_user = result.stopped_by_user or part.stopped_by_user
        result.interrupted = result.interrupted or part.interrupted
        result.shutting_down = result.shutting_down or part.shutting_down
        # ``deferred`` намеренно НЕ сводится: у ящика он значит «моя квота на
        # сегодня кончилась», у кампании — «остаток перенесён на завтра».
        # Ящик может упереться в лимит на последнем адресе, когда рассылка уже
        # закончена, — переносить тогда нечего. Решение принимает верхний
        # уровень по непустой очереди адресов (capped_by_limit).


def _is_fatal(exc: SmtpError) -> bool:
    from cbmail.smtp.sender import SmtpConnectionError

    return isinstance(exc, SmtpConnectionError)


def _log_success(
    session: Session,
    campaign_id: int,
    email: str,
    *,
    now: Callable[[], datetime],
    smtp_id: int | None = None,
) -> None:
    # Ящик записываем в сам лог: суточный лимит принадлежит ящику, а рассылка
    # идёт с нескольких сразу — по кампании чужую квоту уже не отличить.
    session.add(
        CampaignLog(campaign_id=campaign_id, email=email, status=LOG_SENT, smtp_id=smtp_id)
    )
    session.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id)
        .values(sent_count=Campaign.sent_count + 1, updated_at=now())
    )
    # Коммит на каждое письмо: при падении процесса уже отправленные письма
    # обязаны остаться зафиксированными, иначе при перезапуске они уйдут повторно.
    session.commit()


def _log_failure(
    session: Session,
    campaign_id: int,
    email: str,
    exc: Exception,
    *,
    now: Callable[[], datetime],
    smtp_id: int | None = None,
) -> None:
    session.add(
        CampaignLog(
            campaign_id=campaign_id,
            email=email,
            status=LOG_FAILED,
            error_message=str(exc)[:2000],
            smtp_id=smtp_id,
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
    """Забрать наступившие кампании в очередь и вернуть их идентификаторы.

    Условие строгое: статус «Запланирована» И время задано И оно наступило.
    Отсутствие времени никогда не означает «отправить сейчас».

    Отбор и пометка — одним ``UPDATE ... RETURNING``, и это не оптимизация.
    Раньше тик только читал список, ничего не меняя в базе: пока все места
    воркера заняты, одна и та же кампания уходила в очередь заново каждую
    минуту. За час простоя там накапливались десятки копий одной задачи, а
    очередь разбирается по порядку — освободившееся место доставало самую
    старую копию вместо того, что человек запустил только что. Кампания,
    которую остановили и запустили заново, оказывалась в хвосте за этими
    копиями и не стартовала вовсе. Помеченную «В очереди» следующий тик уже не
    видит, поэтому дубликатов не возникает.
    """
    from cbmail.domain.campaign import SCHEDULE_TOLERANCE_SECONDS

    threshold = now + timedelta(seconds=SCHEDULE_TOLERANCE_SECONDS)
    rows = (
        session.execute(
            update(Campaign)
            .where(
                Campaign.status == CampaignStatus.SCHEDULED.value,
                Campaign.scheduled_time.is_not(None),
                Campaign.scheduled_time <= threshold,
            )
            .values(status=CampaignStatus.QUEUED.value, updated_at=now)
            .returning(Campaign.id)
        )
        .scalars()
        .all()
    )
    session.commit()
    return list(rows)


def _as_utc(value: datetime | None) -> datetime | None:
    """Привести отметку времени из БД к UTC-aware.

    SQLite возвращает naive datetime, Postgres — aware. Сравнивать их напрямую
    нельзя: Python бросает TypeError.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def recover_stuck_campaigns(session: Session, now: datetime) -> list[int]:
    """Вернуть в работу кампании, брошенные упавшим или перезапущенным воркером.

    Возвращаем в «Запланирована» со временем «сейчас», а НЕ в «Черновик».
    Черновик расписание не видит: кампания, прерванная выкаткой посреди
    рассылки, оставалась лежать с половиной разосланных писем и не
    возобновлялась, пока человек сам её не замечал. Уже отправленные адреса
    отфильтруются при следующем заходе, так что повторных писем это не даёт.

    Зависнуть можно в двух местах:

    * «Отправляется» без признаков жизни — воркера убили посреди отправки.
      Порог у каждой кампании свой, по её интервалу между письмами: при паузе
      в полчаса молчание в двадцать минут — это норма, а не зависание. Общий
      порог по самой медленной кампании маскировал настоящие зависания всех
      остальных.
    * «В очереди» дольше разумного — задача не доехала до воркера (перезапуск
      брокера, потерянное сообщение).
    """
    recovered: list[int] = []

    sending = session.execute(
        select(Campaign.id, Campaign.message_interval, Campaign.updated_at).where(
            Campaign.status == CampaignStatus.SENDING.value
        )
    ).all()
    for campaign_id, interval, updated_at in sending:
        headroom = max(
            ZOMBIE_BASE_MINUTES, (interval or 0) // 60 + ZOMBIE_HEADROOM_MINUTES
        )
        last_seen = _as_utc(updated_at)
        if last_seen is not None and last_seen >= now - timedelta(minutes=headroom):
            continue
        logger.warning("Кампания %s зависла в отправке — возвращаю в расписание", campaign_id)
        session.add(
            CampaignLog(
                campaign_id=campaign_id,
                email=LOG_SYSTEM,
                status=LOG_FAILED,
                error_message=(
                    f"Брошена в статусе «Отправляется» дольше {headroom} мин — "
                    "возвращена в расписание, остаток уйдёт автоматически"
                ),
            )
        )
        recovered.append(campaign_id)

    stale_queue = now - timedelta(minutes=QUEUE_STALE_MINUTES)
    queued = session.execute(
        select(Campaign.id, Campaign.updated_at).where(
            Campaign.status == CampaignStatus.QUEUED.value
        )
    ).all()
    for campaign_id, updated_at in queued:
        last_seen = _as_utc(updated_at)
        if last_seen is not None and last_seen >= stale_queue:
            continue
        logger.warning("Кампания %s застряла в очереди — возвращаю в расписание", campaign_id)
        session.add(
            CampaignLog(
                campaign_id=campaign_id,
                email=LOG_SYSTEM,
                status=LOG_FAILED,
                error_message=(
                    f"Простояла в очереди дольше {QUEUE_STALE_MINUTES} мин — "
                    "задача потеряна, кампания возвращена в расписание"
                ),
            )
        )
        recovered.append(campaign_id)

    for campaign_id in recovered:
        session.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(
                status=CampaignStatus.SCHEDULED.value,
                scheduled_time=now,
                run_token=None,
                updated_at=now,
            )
        )
    session.commit()
    return recovered


def iter_recipients_preview(campaign: Campaign, limit: int = 50) -> Iterator[str]:
    """Первые адреса кампании — для интерфейса, без загрузки всего списка."""
    for index, email in enumerate(parse_emails(campaign.recipient_emails)):
        if index >= limit:
            return
        yield email


def send_test_email(session: Session, campaign: Campaign, recipient: str) -> None:
    """Отправить одно письмо кампании на указанный адрес.

    Проверка перед рассылкой: увидеть письмо своими глазами в реальном почтовом
    клиенте — единственный способ заметить поехавшую вёрстку, неподставленное
    имя или подпись не того отправителя. Предпросмотр в браузере этого не
    показывает: почтовые клиенты режут стили по-своему.

    Отличия от боевой отправки, и каждое намеренно:

    * ничего не пишется в журнал кампании — иначе адрес попал бы в «уже
      отправлено» и на рассылке его бы пропустили;
    * счётчики кампании не трогаются: тестовое письмо не часть статистики;
    * трекинг не встраивается — открытие проверочного письма не должно
      попадать в отчёт;
    * суточный лимит ящика не расходуется на проверку.

    Исключения (``SmtpError``, ``CampaignNotSendable``) пробрасываются: текст
    ошибки нужно показать человеку рядом с кнопкой, а не прятать в журнал.
    """
    credentials, from_email, _profile = _resolve_smtp(
        session, campaign, now=lambda: datetime.now(timezone.utc)
    )
    message = _compose(campaign)
    personalized = message.personalize(
        recipient,
        composer.build_unsubscribe_url(_base_url(), recipient, scope=email_domain(from_email)),
    )
    outgoing = OutgoingMessage(
        subject=f"[Проверка] {personalized.subject}",
        text_body=personalized.text_body,
        html_body=personalized.html_body,
        from_email=from_email,
        attachments=_load_attachments(session, campaign),
    )
    with SmtpSession(credentials) as smtp:
        smtp.send(outgoing, recipient)
