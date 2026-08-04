"""Проверка, что мы правильно читаем УНАСЛЕДОВАННЫЕ пароли.

Приложение от Django не зависит: в рантайме пароль проверяется чистым
``hashlib.pbkdf2_hmac``. Но в проде уже лежат хеши, записанные Django, и
ошибка в разборе их формата означала бы, что ни один существующий
пользователь не сможет войти. Здесь Django выступает эталоном: сверяем свою
реализацию с оригинальной в обе стороны.

Django нужен только этим тестам и в зависимости приложения не входит; после
завершения миграции файл можно удалить вместе с dev-зависимостью.
"""

from __future__ import annotations

import pytest

from cbmail.core import security

django = pytest.importorskip("django", reason="Django нужен только для проверки совместимости")

SECRET = "o3mmg(gie=ed(wda0$7w9vyb=d@+_!i6gm0ql*w+1!i#r9gn=t"


@pytest.fixture(scope="module", autouse=True)
def _configured_django() -> None:
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            SECRET_KEY=SECRET,
            DEFAULT_HASHING_ALGORITHM="sha256",
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.sessions",
            ],
            DATABASES={},
            USE_TZ=True,
        )
        django.setup()


class TestPasswordCompat:
    def test_verifies_hash_produced_by_django(self) -> None:
        """Пароль, захешированный Django, должен проверяться нашим кодом."""
        from django.contrib.auth.hashers import make_password

        encoded = make_password("Пароль-123!")
        assert security.verify_password("Пароль-123!", encoded)
        assert not security.verify_password("неверный", encoded)

    def test_django_verifies_our_hash(self) -> None:
        """И наоборот: пароль, установленный у нас, должен принимать Django.

        Это обязательное условие для смены пароля через новое приложение —
        иначе пользователь не смог бы войти в Django-админку.
        """
        from django.contrib.auth.hashers import check_password

        encoded = security.hash_password("Другой-Пароль-42")
        assert check_password("Другой-Пароль-42", encoded)
        assert not check_password("мимо", encoded)

    def test_hash_format_matches_django(self) -> None:
        from django.contrib.auth.hashers import make_password

        ours = security.hash_password("x")
        theirs = make_password("x")
        assert ours.split("$")[0] == theirs.split("$")[0] == "pbkdf2_sha256"
        # Число итераций должно быть не ниже, чем у Django по умолчанию,
        # иначе мы бы ослабляли защиту при смене пароля.
        assert int(ours.split("$")[1]) >= int(theirs.split("$")[1])

    def test_unsupported_algorithm_raises_instead_of_silently_failing(self) -> None:
        with pytest.raises(security.UnsupportedHashError):
            security.verify_password("x", "argon2$whatever$salt$hash")

    def test_empty_password_never_matches(self) -> None:
        from django.contrib.auth.hashers import make_password

        assert not security.verify_password("", make_password("real"))


class TestSessionAuthHash:
    """Отпечаток пароля — свой, Django-совместимость здесь не требуется."""

    def test_changes_when_password_changes(self) -> None:
        """Смена пароля обязана обесценивать выданные ранее сессии.

        Иначе украденная сессия переживала бы смену пароля, то есть смена
        пароля не помогала бы против уже случившегося угона.
        """
        old_hash = security.hash_password("старый", iterations=1000)
        new_hash = security.hash_password("новый", iterations=1000)

        assert security.session_auth_hash(old_hash, SECRET) != security.session_auth_hash(
            new_hash, SECRET
        )

    def test_stable_for_same_password_hash(self) -> None:
        password_hash = security.hash_password("пароль", iterations=1000)
        assert security.session_auth_hash(password_hash, SECRET) == security.session_auth_hash(
            password_hash, SECRET
        )

    def test_depends_on_application_secret(self) -> None:
        password_hash = security.hash_password("пароль", iterations=1000)
        assert security.session_auth_hash(password_hash, SECRET) != security.session_auth_hash(
            password_hash, "другой-секрет"
        )


class TestCsrf:
    def test_valid_token_passes(self) -> None:
        key = security.new_session_key()
        token = security.issue_csrf_token(key, SECRET)
        assert security.validate_csrf_token(token, key, SECRET)

    def test_token_is_bound_to_session(self) -> None:
        """Токен от чужой сессии не подходит — в этом смысл привязки."""
        token = security.issue_csrf_token(security.new_session_key(), SECRET)
        other_session = security.new_session_key()
        assert not security.validate_csrf_token(token, other_session, SECRET)

    def test_missing_or_malformed_token_fails(self) -> None:
        key = security.new_session_key()
        assert not security.validate_csrf_token(None, key, SECRET)
        assert not security.validate_csrf_token("", key, SECRET)
        assert not security.validate_csrf_token("мусор", key, SECRET)

    def test_tokens_are_unique_per_issue(self) -> None:
        key = security.new_session_key()
        assert security.issue_csrf_token(key, SECRET) != security.issue_csrf_token(key, SECRET)

    def test_session_keys_are_unpredictable(self) -> None:
        """Ключ сессии — единственное, что защищает её от угадывания."""
        keys = {security.new_session_key() for _ in range(100)}
        assert len(keys) == 100
        # token_urlsafe(32) → 256 бит энтропии, 43 символа в base64url.
        assert all(len(key) >= 43 for key in keys)
