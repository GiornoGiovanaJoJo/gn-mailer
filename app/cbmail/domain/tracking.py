"""Трекинг рассылки: открытия писем и переходы по ссылкам.

Счётчики «открыто» и «переходов» в карточке кампании были всегда нулевыми:
поля в базе есть, а заполнять их некому. Отчёт, который всегда показывает
ноль, хуже отсутствующего — на него смотрят и делают вывод, что рассылка
никому не интересна.

Как устроено:

* В HTML-письмо подставляется прозрачная картинка 1×1. Её загрузка почтовым
  клиентом и есть «открыл».
* Ссылки в письме заменяются на переход через наш адрес, который считает клик
  и сразу отправляет человека дальше.
* И то и другое привязано к паре «кампания + адрес» и подписано: по чужой
  ссылке накрутить счётчик нельзя, а подменить адрес перехода — тем более.

Отдельно про подпись адреса перехода. Без неё ссылка вида
``/t/c/<токен>?u=<адрес>`` — это открытый редирект: кто угодно рассылал бы
фишинг с нашего домена, и репутация домена сгорела бы за сутки. Поэтому
подписывается не только получатель, но и конкретный адрес назначения.

Модуль чистый: ни базы, ни запросов — только подписи и работа со строкой.
Благодаря этому поведение проверяется тестами целиком.
"""

from __future__ import annotations

import base64
import hmac
import re
from hashlib import sha256
from urllib.parse import quote

#: Прозрачный GIF 1×1 — то, что отдаётся на запрос пикселя.
PIXEL_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

#: Плейсхолдер отписки, который пользователь мог вписать в шаблон руками.
_UNSUBSCRIBE_PATTERN = re.compile(r"\{\{\s*unsubscribe(?:_url)?\s*\}\}", re.IGNORECASE)

#: Ссылки в письме. Берём только http(s): mailto и tel трогать нельзя.
_HREF_PATTERN = re.compile(r'href\s*=\s*"(https?://[^"]+)"', re.IGNORECASE)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign(secret: str, payload: str) -> str:
    """Подпись строки ключом приложения."""
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), sha256).digest()
    return _b64(digest)


def _same(expected: str, given: str) -> bool:
    """Сравнение подписей за постоянное время.

    Сравниваем байты, а не строки: ``hmac.compare_digest`` на строках с
    не-ASCII символами бросает TypeError, а подпись приходит из адреса письма —
    туда можно подставить что угодно. Ссылка вида ``?s=подделка`` роняла бы
    обработчик пятисотой вместо честного «некорректная ссылка».
    """
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))


def make_token(secret: str, campaign_id: int, email: str) -> str:
    """Токен получателя: «кто и в какой кампании», плюс подпись."""
    payload = _b64(f"{campaign_id}:{email}".encode())
    return f"{payload}.{sign(secret, payload)}"


def parse_token(secret: str, token: str) -> tuple[int, str] | None:
    """Разобрать токен. ``None`` — подпись не сошлась или мусор."""
    payload, _, signature = (token or "").partition(".")
    if not payload or not signature:
        return None
    if not _same(sign(secret, payload), signature):
        return None
    try:
        decoded = _unb64(payload).decode("utf-8")
    except Exception:
        return None
    campaign_raw, _, email = decoded.partition(":")
    if not email or not campaign_raw.isdigit():
        return None
    return int(campaign_raw), email


def click_signature(secret: str, token: str, url: str) -> str:
    """Подпись конкретного перехода: токен получателя + адрес назначения."""
    return sign(secret, f"{token}|{url}")


def verify_click(secret: str, token: str, url: str, signature: str) -> bool:
    if not signature:
        return False
    return _same(click_signature(secret, token, url), signature)


def pixel_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/t/o/{token}.gif"


def click_url(base_url: str, token: str, secret: str, target: str) -> str:
    signature = click_signature(secret, token, target)
    return f"{base_url.rstrip('/')}/t/c/{token}?u={quote(target, safe='')}&s={signature}"


def inject(
    html: str | None,
    *,
    base_url: str,
    secret: str,
    campaign_id: int,
    email: str,
    unsubscribe_url: str | None = None,
    track_opens: bool = True,
    track_clicks: bool = True,
) -> str | None:
    """Вставить в письмо пиксель и переписать ссылки на переходы.

    Возвращает исходный HTML без изменений, если письма нет или неизвестен
    публичный адрес сервиса: ссылка на несуществующий хост в письме хуже
    отсутствующей статистики — по ней человек попадёт в никуда.
    """
    if not html:
        return html

    out = html
    if unsubscribe_url:
        out = _UNSUBSCRIBE_PATTERN.sub(unsubscribe_url, out)

    base = (base_url or "").rstrip("/")
    if not base:
        return out

    token = make_token(secret, campaign_id, email)

    if track_clicks:
        def replace(match: re.Match[str]) -> str:
            url = match.group(1)
            # Свои же ссылки — отписка и сам трекинг — заворачивать нельзя:
            # переход считался бы кликом по рассылке, а отписка ломалась бы.
            if url.startswith(f"{base}/t/") or url.startswith(f"{base}/unsubscribe"):
                return match.group(0)
            return f'href="{click_url(base, token, secret, url)}"'

        out = _HREF_PATTERN.sub(replace, out)

    if track_opens:
        pixel = (
            f'<img src="{pixel_url(base, token)}" width="1" height="1" alt=""'
            ' style="display:none" />'
        )
        # Перед </body>, если тег есть: письма без него встречаются, и тогда
        # пиксель просто дописывается в конец.
        out = out.replace("</body>", f"{pixel}</body>", 1) if "</body>" in out else out + pixel

    return out
