"""Пароли, сессии и CSRF.

Модуль не импортирует Django и не зависит от него.

Единственное наследие, которое приходится поддерживать, — ФОРМАТ уже
записанных паролей: в проде они лежат как ``pbkdf2_sha256$<iters>$<salt>$<hash>``.
PBKDF2-HMAC-SHA256 — стандартный алгоритм из ``hashlib``, а не технология
Django; читать его нужно, чтобы существующие пользователи не потеряли доступ.
Новые пароли пишутся в том же формате: он актуален и менять его незачем.

Сессии и CSRF — собственные. Сессия хранится в своей таблице и адресуется
непредсказуемым ключом; в cookie уходит только ключ, никаких данных
пользователя. CSRF-токен привязан к сессии, поэтому подсунуть его из другого
контекста нельзя.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# --- Пароли -----------------------------------------------------------------

_PBKDF2_ALGORITHM = "pbkdf2_sha256"
# Значение Django 5.2 по умолчанию. Используется только для НОВЫХ паролей;
# существующие проверяются с тем числом итераций, что записано в самом хеше.
_DEFAULT_ITERATIONS = 1_000_000


class UnsupportedHashError(Exception):
    """Хеш пароля записан алгоритмом, который мы не умеем проверять."""


def verify_password(password: str, encoded: str) -> bool:
    """Проверить пароль против Django-хеша.

    Поддерживается ``pbkdf2_sha256`` — единственный алгоритм, которым Django
    хеширует пароли по умолчанию и которым записаны все существующие учётки.
    Для любого другого префикса возбуждается исключение: молча возвращать
    ``False`` нельзя — это выглядело бы как «неверный пароль» и увело бы
    диагностику в сторону.
    """
    if not encoded or not password:
        return False

    try:
        algorithm, iterations_raw, salt, expected_hash = encoded.split("$", 3)
    except ValueError:
        raise UnsupportedHashError(f"Неразборный формат хеша пароля: {encoded[:20]}…") from None

    if algorithm != _PBKDF2_ALGORITHM:
        raise UnsupportedHashError(f"Неподдерживаемый алгоритм хеширования: {algorithm}")

    try:
        iterations = int(iterations_raw)
    except ValueError:
        raise UnsupportedHashError("Некорректное число итераций в хеше пароля") from None

    computed = _pbkdf2(password, salt, iterations)
    # Сравнение постоянного времени: обычное `==` утекает длину общего префикса
    # и делает возможным подбор хеша по времени ответа.
    return hmac.compare_digest(computed, expected_hash)


def hash_password(password: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """Захешировать пароль в формате, который поймёт и Django-админка."""
    salt = secrets.token_hex(16)
    digest = _pbkdf2(password, salt, iterations)
    return f"{_PBKDF2_ALGORITHM}${iterations}${salt}${digest}"


def _pbkdf2(password: str, salt: str, iterations: int) -> str:
    raw = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return base64.b64encode(raw).decode().strip()


def needs_rehash(encoded: str, *, iterations: int = _DEFAULT_ITERATIONS) -> bool:
    """Пароль захеширован слабее текущего стандарта — стоит перехешировать при входе."""
    try:
        algorithm, iterations_raw, _, _ = encoded.split("$", 3)
    except ValueError:
        return True
    if algorithm != _PBKDF2_ALGORITHM:
        return True
    try:
        return int(iterations_raw) < iterations
    except ValueError:
        return True


# --- Подпись значений --------------------------------------------------------

_SEPARATOR = ":"


class BadSignature(Exception):
    """Подпись не сошлась — данные подделаны или ключ другой."""


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _salted_hmac(key_salt: str, value: str, secret: str) -> hmac.HMAC:
    """HMAC-SHA256 с ключом, выведенным из секрета и соли назначения.

    Соль разделяет назначения: токен, подписанный для CSRF, не пройдёт проверку
    там, где ждут отпечаток пароля, даже при одном и том же секрете приложения.
    """
    key = hashlib.sha256((key_salt + secret).encode()).digest()
    return hmac.new(key, msg=value.encode(), digestmod=hashlib.sha256)


def _signature(key_salt: str, value: str, secret: str) -> str:
    return _b64_encode(_salted_hmac(key_salt + "signer", value, secret).digest())


_AUTH_HASH_SALT = "cbmail.session.auth_hash"


def session_auth_hash(password_hash: str, secret: str) -> str:
    """Отпечаток пароля, привязываемый к сессии.

    Позволяет разлогинить все устройства при смене пароля: если отпечаток,
    сохранённый в сессии, не сходится с текущим паролем, сессия считается
    недействительной. Без этого угнанная сессия переживала бы смену пароля —
    то есть смена пароля не помогала бы против уже случившегося угона.
    """
    return _salted_hmac(_AUTH_HASH_SALT, password_hash, secret).hexdigest()


def new_session_key() -> str:
    """Непредсказуемый ключ сессии.

    ``secrets.token_urlsafe(32)`` даёт 256 бит энтропии. Сами данные сессии
    в cookie не уезжают — только этот ключ, по которому запись ищется в БД,
    поэтому подделать содержимое сессии, не имея доступа к базе, нельзя.
    """
    return secrets.token_urlsafe(32)


# --- CSRF -------------------------------------------------------------------

CSRF_COOKIE_NAME = "cbmail_csrf"
CSRF_FIELD_NAME = "csrfmiddlewaretoken"
CSRF_HEADER_NAME = "x-csrftoken"
_CSRF_SALT = "cbmail.csrf"


def issue_csrf_token(session_key: str, secret: str) -> str:
    """Выдать CSRF-токен, привязанный к сессии.

    Привязка к ключу сессии — принципиальное отличие от схемы Django с
    независимой cookie: украденный или подсунутый через поддомен токен не
    подойдёт к чужой сессии.
    """
    nonce = secrets.token_urlsafe(16)
    signature = _signature(_CSRF_SALT, f"{session_key}{_SEPARATOR}{nonce}", secret)
    return f"{nonce}{_SEPARATOR}{signature}"


def validate_csrf_token(token: str | None, session_key: str, secret: str) -> bool:
    if not token or not session_key:
        return False
    try:
        nonce, signature = token.rsplit(_SEPARATOR, 1)
    except ValueError:
        return False
    expected = _signature(_CSRF_SALT, f"{session_key}{_SEPARATOR}{nonce}", secret)
    return hmac.compare_digest(expected, signature)
