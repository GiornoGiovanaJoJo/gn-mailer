"""Отправка почты через SMTP.

Отличия от Django-версии, каждое из которых лечит конкретный дефект:

* **Одно соединение на пачку писем.** Раньше перед КАЖДЫМ письмом выполнялся
  отдельный «тестовый» коннект с полной авторизацией, а следом — второй, уже
  рабочий. Это удваивало число логинов, замедляло рассылку вдвое и приводило к
  временным блокировкам со стороны провайдеров за слишком частые авторизации.
* **Никакого ``set_debuglevel``.** Он печатал в лог весь SMTP-диалог, включая
  base64-строку с логином и паролем.
* **Различаем временные и постоянные ошибки.** Код 4xx (переполнен ящик,
  greylisting) — повод повторить позже; 5xx (адрес не существует) — повод
  больше не пытаться. Раньше любая ошибка одинаково писалась как ``failed``,
  и временный сбой навсегда терял получателя.
* **Текстовая версия письма не добывается из HTML.** ``strip_tags`` оставлял
  склеенную кашу из CSS и разметки, которую и видели пользователи текстовых
  клиентов.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_TLS_PORT = 587
DEFAULT_SSL_PORT = 465


class SmtpError(Exception):
    """Базовая ошибка отправки."""

    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent
        """Постоянная ошибка: повторять бессмысленно, адрес нужно исключить."""


class SmtpAuthError(SmtpError):
    """Не удалось авторизоваться — проблема в настройках профиля, не в адресате."""

    def __init__(self, message: str) -> None:
        super().__init__(message, permanent=True)


class SmtpConnectionError(SmtpError):
    """Сервер недоступен. Всегда временная — сеть может восстановиться."""

    def __init__(self, message: str) -> None:
        super().__init__(message, permanent=False)


class SmtpRecipientError(SmtpError):
    """Сервер отказал конкретному получателю."""


@dataclass(frozen=True, slots=True)
class SmtpCredentials:
    """Параметры подключения, отвязанные от модели БД.

    Отдельный тип нужен по двум причинам: отправку можно тестировать без базы,
    и — важнее — старый код МУТИРОВАЛ объект настроек прямо перед отправкой
    («починим порт для Beget»), после чего изменения могли уехать в БД и
    молча испортить сохранённый профиль.
    """

    host: str
    port: int
    username: str
    password: str
    use_tls: bool = True
    use_ssl: bool = False
    timeout: int = DEFAULT_TIMEOUT

    @classmethod
    def from_profile(cls, profile: object) -> SmtpCredentials:
        """Собрать параметры из ORM-модели профиля, ничего в ней не меняя."""
        host = (getattr(profile, "email_host", "") or "").strip()
        if not host:
            raise SmtpAuthError("В профиле почты не указан SMTP-сервер")

        use_ssl = bool(getattr(profile, "email_use_ssl", False))
        use_tls = bool(getattr(profile, "email_use_tls", False))
        raw_port = getattr(profile, "email_port", None)

        return cls(
            host=host,
            port=_parse_port(raw_port, use_ssl=use_ssl),
            username=(getattr(profile, "email_host_user", "") or "").strip(),
            password=getattr(profile, "email_host_password", "") or "",
            use_tls=use_tls and not use_ssl,
            use_ssl=use_ssl,
        )

    def masked(self) -> str:
        """Представление для логов — без пароля."""
        return f"{self.username}@{self.host}:{self.port}"


def _parse_port(raw: object, *, use_ssl: bool) -> int:
    """Порт хранится как текст и часто содержит мусор вроде ``'587 '`` или ``''``.

    Раньше ``int(port)`` падал в общем ``try``, и вся отправка молча
    прекращалась с невнятным сообщением.
    """
    default = DEFAULT_SSL_PORT if use_ssl else DEFAULT_TLS_PORT
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        port = int(text)
    except ValueError:
        logger.warning("Некорректный порт SMTP %r — использую %d", raw, default)
        return default
    if not 1 <= port <= 65535:
        logger.warning("Порт SMTP вне диапазона: %d — использую %d", port, default)
        return default
    return port


@dataclass(slots=True)
class Attachment:
    filename: str
    content: bytes
    mime_type: str = "application/octet-stream"

    @property
    def maintype(self) -> str:
        return self.mime_type.split("/", 1)[0]

    @property
    def subtype(self) -> str:
        parts = self.mime_type.split("/", 1)
        return parts[1] if len(parts) > 1 else "octet-stream"


@dataclass(slots=True)
class OutgoingMessage:
    subject: str
    text_body: str
    html_body: str | None = None
    from_email: str = ""
    from_name: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    def build(self, to: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = self.subject
        message["From"] = (
            formataddr((self.from_name, self.from_email)) if self.from_name else self.from_email
        )
        message["To"] = to
        # Message-ID нужен, чтобы почтовые клиенты не склеивали разные письма
        # в один тред, а antispam-фильтры не считали письмо подозрительным.
        message["Message-ID"] = make_msgid(domain=self.from_email.rsplit("@", 1)[-1] or None)
        for name, value in self.headers.items():
            message[name] = value

        message.set_content(self.text_body or "")
        if self.html_body:
            message.add_alternative(self.html_body, subtype="html")

        for attachment in self.attachments:
            message.add_attachment(
                attachment.content,
                maintype=attachment.maintype,
                subtype=attachment.subtype,
                filename=attachment.filename,
            )
        return message


# --- Провайдеры со строгой проверкой отправителя ------------------------------

# Эти сервисы требуют, чтобы MAIL FROM совпадал с авторизованным ящиком, иначе
# отдают "5.7.1 Sender address rejected". Для них адрес отправителя
# принудительно заменяется на логин.
_STRICT_FROM_HOSTS = (
    "yandex",
    "mail.ru",
    "gmail",
    "beget",
    "rambler",
    "yahoo",
)


def is_strict_from_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower()
    return any(marker in host for marker in _STRICT_FROM_HOSTS)


def resolve_from_email(credentials: SmtpCredentials, requested: str | None) -> str:
    """Определить адрес в поле From.

    Для строгих провайдеров возвращается логин, иначе — запрошенный адрес.
    Проверка идёт по подстроке в имени хоста, поэтому ``smtp.yandex.ru`` и
    ``smtp.yandex.com`` покрываются одним правилом — раньше для каждого домена
    была отдельная строка в списке, и ``smtp.yandex.by`` в него не попадал.
    """
    if is_strict_from_host(credentials.host):
        return credentials.username
    return (requested or "").strip() or credentials.username


class SmtpSession:
    """Одно SMTP-соединение, переиспользуемое для пачки писем.

    Работает как контекстный менеджер: соединение гарантированно закрывается
    даже при исключении — в старой версии ``connection.close()`` стоял в теле
    ``try`` и при падении соединение утекало до таймаута сервера.
    """

    def __init__(self, credentials: SmtpCredentials) -> None:
        self._credentials = credentials
        self._client: smtplib.SMTP | smtplib.SMTP_SSL | None = None

    def __enter__(self) -> SmtpSession:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def connect(self) -> None:
        creds = self._credentials
        # Проверка сертификата включена по умолчанию: незашифрованная или
        # непроверенная сессия означает, что пароль от почтового ящика уходит
        # по сети в открытом виде.
        context = ssl.create_default_context()
        try:
            if creds.use_ssl:
                self._client = smtplib.SMTP_SSL(
                    creds.host, creds.port, context=context, timeout=creds.timeout
                )
            else:
                self._client = smtplib.SMTP(creds.host, creds.port, timeout=creds.timeout)
                self._client.ehlo()
                if creds.use_tls:
                    self._client.starttls(context=context)
                    self._client.ehlo()
        except (smtplib.SMTPConnectError, OSError, ssl.SSLError) as exc:
            raise SmtpConnectionError(f"Не удалось подключиться к {creds.host}: {exc}") from exc

        if creds.username and creds.password:
            try:
                self._client.login(creds.username, creds.password)
            except smtplib.SMTPAuthenticationError as exc:
                self.close()
                raise SmtpAuthError(
                    f"Сервер отклонил логин {creds.username}: {_describe(exc)}"
                ) from exc
            except smtplib.SMTPException as exc:
                self.close()
                raise SmtpConnectionError(f"Ошибка авторизации: {exc}") from exc

    def send(self, message: OutgoingMessage, to: str) -> None:
        """Отправить одно письмо. Возбуждает :class:`SmtpError` при отказе."""
        if self._client is None:
            raise SmtpConnectionError("Соединение не установлено")

        built = message.build(to)
        try:
            self._client.send_message(built)
        except smtplib.SMTPRecipientsRefused as exc:
            code = _first_code(exc.recipients)
            raise SmtpRecipientError(
                f"Получатель отклонён: {_describe(exc)}", permanent=_is_permanent(code)
            ) from exc
        except smtplib.SMTPSenderRefused as exc:
            raise SmtpRecipientError(
                f"Отправитель отклонён сервером: {_describe(exc)}",
                permanent=_is_permanent(exc.smtp_code),
            ) from exc
        except smtplib.SMTPResponseException as exc:
            raise SmtpRecipientError(
                f"Сервер вернул ошибку: {_describe(exc)}", permanent=_is_permanent(exc.smtp_code)
            ) from exc
        except (smtplib.SMTPServerDisconnected, OSError) as exc:
            # Разрыв соединения — временная проблема; пачку можно повторить.
            raise SmtpConnectionError(f"Соединение разорвано: {exc}") from exc

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.quit()
        except (smtplib.SMTPException, OSError):
            # Сервер мог уже закрыть канал — это не ошибка отправки.
            with contextlib.suppress(OSError):
                self._client.close()
        finally:
            self._client = None


def verify_credentials(credentials: SmtpCredentials) -> tuple[bool, str]:
    """Проверить настройки: подключиться, авторизоваться, отключиться.

    Используется кнопкой «Проверить подключение» и НЕ вызывается перед каждой
    отправкой — именно это было причиной удвоенного числа авторизаций.
    """
    try:
        with SmtpSession(credentials):
            return True, "Подключение успешно"
    except SmtpError as exc:
        logger.info("Проверка SMTP %s не пройдена: %s", credentials.masked(), exc)
        return False, str(exc)


async def verify_credentials_async(credentials: SmtpCredentials) -> tuple[bool, str]:
    """Неблокирующая обёртка для веб-обработчика.

    ``smtplib`` синхронный, а подключение к недоступному хосту занимает до
    ``timeout`` секунд. Без выноса в поток такой запрос вешал бы весь event loop.
    """
    return await asyncio.to_thread(verify_credentials, credentials)


def _is_permanent(code: int | None) -> bool:
    """5xx — постоянный отказ, 4xx — временный.

    Неизвестный код считаем временным: лучше повторить лишний раз, чем
    навсегда потерять действительного получателя.
    """
    return code is not None and 500 <= code < 600


def _first_code(recipients: dict[str, tuple[int, bytes]]) -> int | None:
    for code, _ in recipients.values():
        return code
    return None


_CREDENTIAL_RE = re.compile(r"(?i)(AUTH\s+\w+\s+)\S+")


def _describe(exc: smtplib.SMTPException) -> str:
    """Текст ошибки сервера без учётных данных.

    Ответ сервера при неудачной авторизации иногда содержит присланную строку
    AUTH — попадание её в лог означало бы пароль в открытом виде.
    """
    code = getattr(exc, "smtp_code", None)
    raw = getattr(exc, "smtp_error", None)
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or exc)
    text = _CREDENTIAL_RE.sub(r"\1***", text)
    return f"{code} {text}".strip() if code else text
