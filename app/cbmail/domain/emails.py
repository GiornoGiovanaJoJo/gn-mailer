"""Работа с email-адресами: разбор, нормализация, сравнение.

Единственное место в системе, которое решает, что такое «адрес» и когда два
адреса считаются одинаковыми. В Django-версии этих правил было пять разных:

* ``Message.get_clients_list`` дедуплицировал по ``lower()``, сохраняя порядок;
* ``MassMailCampaign.get_recipients_list`` не дедуплицировал вообще — из-за чего
  один и тот же адрес, указанный дважды, получал два письма;
* ``StopList.add_email`` сравнивал регистрозависимо, а фильтрация при отправке —
  через ``lower()``, поэтому ``Ivan@mail.ru`` в стоп-листе не блокировал
  ``ivan@mail.ru`` при добавлении, но блокировал при отправке;
* ``check_mass_mail_campaigns`` сравнивал уже отправленные адреса точным
  совпадением, а стоп-лист — через ``lower()``;
* валидность адреса определялась как ``'@' in part``.

Здесь правило одно: адрес нормализуется приведением домена к нижнему регистру,
сравнение всегда идёт по нормализованной форме, а отображается исходная.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

# Разделители в пользовательском вводе: запятая, перевод строки, точка с запятой,
# любые пробельные символы. Точка с запятой — частый разделитель при копировании
# из Outlook, старый парсер её не понимал и склеивал два адреса в один токен.
_SPLIT_RE = re.compile(r"[,;\s]+")

# Прагматичная проверка адреса. Намеренно не реализуем RFC 5322 целиком: полный
# грамматический разбор пропускает экзотику, которую всё равно отвергнет relay,
# и усложняет код. Требуем непустую локальную часть, ровно одну @, домен с точкой
# и валидным TLD — это отсекает и `'@' in part`-мусор вроде "@" или "a@b".
_EMAIL_RE = re.compile(
    r"^(?P<local>[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+)"
    r"@"
    r"(?P<domain>(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63})$"
)

MAX_EMAIL_LENGTH = 254  # RFC 5321 §4.5.3.1.3
MAX_LOCAL_LENGTH = 64


def is_valid_email(value: str) -> bool:
    """Проверка адреса без побочных эффектов."""
    if not value or len(value) > MAX_EMAIL_LENGTH:
        return False
    match = _EMAIL_RE.match(value)
    if match is None:
        return False
    if len(match.group("local")) > MAX_LOCAL_LENGTH:
        return False
    # Точка не может открывать/закрывать локальную часть или идти подряд.
    local = match.group("local")
    return not (local.startswith(".") or local.endswith(".") or ".." in local)


def normalize_email(value: str) -> str:
    """Каноническая форма для сравнения и хранения ключей.

    Домен регистронезависим по RFC 1035, поэтому приводится к нижнему регистру.
    Локальная часть формально регистрозависима, но на практике все массовые
    почтовые провайдеры её не различают; сравнивать с учётом регистра означало бы
    дважды слать письмо на ``Ivan@mail.ru`` и ``ivan@mail.ru``. Приводим целиком —
    это и есть то поведение, которого ожидал пользователь от стоп-листа.
    """
    return value.strip().lower()


def email_domain(value: str | None) -> str:
    """Домен адреса (часть после ``@``) в нижнем регистре.

    Используется как «область» отписки: человек отписывается от писем
    конкретного отправителя (его домена), а не от всех рассылок сразу.
    Возвращает пустую строку, если ``@`` в адресе нет.
    """
    _, sep, domain = (value or "").strip().rpartition("@")
    return domain.lower() if sep else ""


def parse_emails(raw: str | None) -> list[str]:
    """Разбор пользовательского ввода в список валидных адресов.

    Сохраняет порядок первого появления и убирает дубликаты по нормализованной
    форме. Невалидные токены отбрасываются — вызывающий код, которому нужно
    сообщить о них пользователю, использует :func:`split_emails`.
    """
    return [email for email, _ in _iter_unique_valid(raw)]


def split_emails(raw: str | None) -> tuple[list[str], list[str]]:
    """Как :func:`parse_emails`, но отдельно возвращает отбракованные токены.

    Нужно формам: пользователь должен видеть, что три адреса из его файла не
    приняты, а не молча получить рассылку на меньшее число получателей.
    """
    if not raw:
        return [], []

    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()

    for token in _SPLIT_RE.split(raw):
        token = token.strip().strip("<>,;")
        if not token:
            continue
        if not is_valid_email(token):
            invalid.append(token)
            continue
        key = normalize_email(token)
        if key in seen:
            continue
        seen.add(key)
        valid.append(token)

    return valid, invalid


def _iter_unique_valid(raw: str | None) -> Iterator[tuple[str, str]]:
    if not raw:
        return
    seen: set[str] = set()
    for token in _SPLIT_RE.split(raw):
        token = token.strip().strip("<>,;")
        if not token or not is_valid_email(token):
            continue
        key = normalize_email(token)
        if key in seen:
            continue
        seen.add(key)
        yield token, key


def normalized_set(emails: Iterable[str]) -> set[str]:
    """Множество нормализованных адресов — для операций включения/исключения."""
    return {normalize_email(email) for email in emails if email}


def exclude(emails: Iterable[str], excluded: Iterable[str]) -> list[str]:
    """Убрать из ``emails`` всё, что есть в ``excluded``, сравнивая нормализованно.

    Порядок оставшихся сохраняется — рассылка идёт в том порядке, в котором
    пользователь перечислил получателей.
    """
    blocked = normalized_set(excluded)
    return [email for email in emails if normalize_email(email) not in blocked]


def join_emails(emails: Iterable[str]) -> str:
    """Обратная операция к :func:`parse_emails` для хранения в текстовом поле.

    Django-версия местами делала ``', '.join(set(...))``, из-за чего порядок
    получателей менялся при каждом сохранении кампании непредсказуемым образом.
    """
    return ", ".join(emails)
