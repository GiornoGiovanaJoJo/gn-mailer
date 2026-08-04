"""Правила жизненного цикла кампании массовой рассылки.

Чистая логика: никаких запросов к БД, SMTP и времени «изнутри» — всё приходит
аргументами. Благодаря этому переходы статусов проверяются юнит-тестами без
поднятия окружения, а сама таблица переходов задана явно, а не разбросана по
вьюхам и management-команде, как было раньше.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class CampaignStatus(str, Enum):
    """Статусы кампании.

    Наследование от ``str`` (а не ``enum.StrEnum``, требующий Python 3.11)
    сохраняет совместимость с 3.10 и позволяет сравнивать значение напрямую
    со строкой из БД, не меняя схему.
    """

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    SENDING = "sending"
    SENT = "sent"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]


_STATUS_LABELS: dict[CampaignStatus, str] = {
    CampaignStatus.DRAFT: "Черновик",
    CampaignStatus.SCHEDULED: "Запланирована",
    CampaignStatus.SENDING: "Отправляется",
    CampaignStatus.SENT: "Отправлена",
    CampaignStatus.CANCELLED: "Отменена",
}


# Явная таблица допустимых переходов. Раньше статус присваивался прямым
# `campaign.status = '...'` в восьми местах, и часть комбинаций была невозможна
# по смыслу, но ничем не запрещена — например, отправленную кампанию можно было
# перевести обратно в «Запланирована» и разослать повторно.
_ALLOWED_TRANSITIONS: dict[CampaignStatus, frozenset[CampaignStatus]] = {
    CampaignStatus.DRAFT: frozenset({CampaignStatus.SCHEDULED, CampaignStatus.SENDING}),
    CampaignStatus.SCHEDULED: frozenset(
        {CampaignStatus.DRAFT, CampaignStatus.SENDING, CampaignStatus.CANCELLED}
    ),
    CampaignStatus.SENDING: frozenset(
        {CampaignStatus.SENT, CampaignStatus.CANCELLED, CampaignStatus.DRAFT}
    ),
    # Из финальных состояний выйти можно только явным решением пользователя
    # «отправить заново», которое сбрасывает кампанию в черновик.
    CampaignStatus.SENT: frozenset({CampaignStatus.DRAFT}),
    CampaignStatus.CANCELLED: frozenset({CampaignStatus.DRAFT, CampaignStatus.SCHEDULED}),
}


class InvalidTransition(Exception):
    """Попытка перевести кампанию в недопустимый статус."""

    def __init__(self, current: CampaignStatus, target: CampaignStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"Недопустимый переход статуса кампании: {current.label} → {target.label}"
        )


def can_transition(current: CampaignStatus, target: CampaignStatus) -> bool:
    if current == target:
        return True
    return target in _ALLOWED_TRANSITIONS[current]


def ensure_transition(current: CampaignStatus, target: CampaignStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


# --- Ограничения параметров отправки ---------------------------------------

MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 1000
MIN_INTERVAL_SECONDS = 0
MAX_INTERVAL_SECONDS = 86_400  # сутки
DEFAULT_BATCH_SIZE = 50
DEFAULT_INTERVAL_SECONDS = 2


def clamp_batch_size(value: object) -> int:
    return _clamp_int(value, DEFAULT_BATCH_SIZE, MIN_BATCH_SIZE, MAX_BATCH_SIZE)


def clamp_interval(value: object) -> int:
    return _clamp_int(
        value, DEFAULT_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS
    )


def _clamp_int(value: object, default: int, low: int, high: int) -> int:
    """Привести пользовательский ввод к целому в допустимом диапазоне.

    Принимает и ``"2.5"`` — поле в форме числовое, но браузеры и локали
    подставляют дробные значения; старый код на этом падал в ``ValueError``,
    который перехватывался общим ``except`` и молча сбрасывал ОБА параметра
    (и размер пачки, и интервал) на дефолтные.
    """
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        parsed = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


# --- Планирование -----------------------------------------------------------

# Допуск на расхождение часов между веб-процессом и воркером. Кампания со
# временем в пределах допуска считается наступившей.
SCHEDULE_TOLERANCE_SECONDS = 30


def is_due(scheduled_time: datetime | None, now: datetime) -> bool:
    """Наступило ли время отправки.

    ``None`` означает «не запланирована» и НИКОГДА не значит «отправить сейчас».
    Раньше отсутствие времени трактовалось как «отправить немедленно», из-за чего
    пустое поле в форме приводило к мгновенной рассылке вместо сохранения
    черновика.
    """
    if scheduled_time is None:
        return False
    _ensure_aware(scheduled_time, "scheduled_time")
    _ensure_aware(now, "now")
    return (scheduled_time - now).total_seconds() <= SCHEDULE_TOLERANCE_SECONDS


def _ensure_aware(value: datetime, name: str) -> None:
    """Наивное время — источник ошибок на час-два при переходе на летнее время.

    Падаем громко вместо того, чтобы молча сравнить naive с aware (что в Python
    вообще возбуждает TypeError глубоко внутри) или сравнить два naive в разных
    зонах.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} должен быть timezone-aware, получен naive datetime")


@dataclass(frozen=True, slots=True)
class SendPlan:
    """Что именно будет отправлено в этом запуске.

    Считается один раз перед отправкой и дальше не пересчитывается, поэтому
    прогресс не «прыгает» при возобновлении остановленной кампании.
    """

    recipients: tuple[str, ...]
    """Кому реально шлём в этом запуске (после всех фильтров), в порядке ввода."""

    total: int
    """Полный размер аудитории кампании — знаменатель прогресса."""

    already_sent: int
    """Сколько получили письмо в предыдущих запусках."""

    skipped_stoplist: int
    skipped_invalid: int

    @property
    def is_empty(self) -> bool:
        return not self.recipients

    @property
    def is_complete(self) -> bool:
        """Все адресаты уже получили письмо — отправлять нечего, но это успех."""
        return self.is_empty and self.already_sent > 0

    def batches(self, batch_size: int) -> list[tuple[str, ...]]:
        size = clamp_batch_size(batch_size)
        return [
            self.recipients[i : i + size] for i in range(0, len(self.recipients), size)
        ]


def build_send_plan(
    *,
    raw_recipients: str | None,
    global_stoplist: frozenset[str] | set[str],
    campaign_stoplist: str | None,
    already_sent: frozenset[str] | set[str],
) -> SendPlan:
    """Собрать план отправки из сырых данных кампании.

    Порядок фильтров важен и зафиксирован тестами:

    1. разбор и дедупликация адресов (валидные, в порядке первого появления);
    2. вычитание стоп-листов — глобального и локального для кампании;
    3. вычитание тех, кому уже успешно отправили ранее.

    Шаг 3 обязан идти последним: адрес, попавший в стоп-лист уже ПОСЛЕ успешной
    отправки, не должен считаться «пропущенным по стоп-листу» в статистике.
    """
    from cbmail.domain import emails as email_domain

    valid, invalid = email_domain.split_emails(raw_recipients)

    stoplist = email_domain.normalized_set(global_stoplist)
    stoplist |= email_domain.normalized_set(email_domain.parse_emails(campaign_stoplist))

    after_stoplist = email_domain.exclude(valid, stoplist)
    skipped_stoplist = len(valid) - len(after_stoplist)

    pending = email_domain.exclude(after_stoplist, already_sent)
    already_sent_count = len(after_stoplist) - len(pending)

    return SendPlan(
        recipients=tuple(pending),
        total=len(after_stoplist),
        already_sent=already_sent_count,
        skipped_stoplist=skipped_stoplist,
        skipped_invalid=len(invalid),
    )
