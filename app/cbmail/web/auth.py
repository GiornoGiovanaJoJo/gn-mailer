"""Аутентификация и серверные сессии.

Пароли читаются в формате Django (``pbkdf2_sha256$…``) — существующие
пользователи входят своими паролями без сброса. Сессия хранится в БД
(таблица ``cbmail_session``); в cookie уходит только непредсказуемый ключ.
Смена пожадобится — привязка ``auth_hash`` к паролю делает все прежние сессии
недействительными при смене пароля.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.core.config import get_settings
from cbmail.core.security import (
    new_session_key,
    session_auth_hash,
    verify_password,
)
from cbmail.db.models import Profile, Session


async def authenticate(session: AsyncSession, username: str, password: str) -> Profile | None:
    """Проверить логин/пароль. Возвращает пользователя или ``None``.

    Заблокированный или удалённый пользователь не входит (см. ``Profile.can_login``):
    в Django-версии проверялся только ``is_active``, поэтому снятый модерацией
    сотрудник продолжал пользоваться почтой.
    """
    username = (username or "").strip()
    if not username or not password:
        return None
    user = (
        await session.execute(select(Profile).where(Profile.username == username))
    ).scalar_one_or_none()
    if user is None:
        return None
    try:
        if not verify_password(password, user.password):
            return None
    except Exception:
        return None
    if not user.can_login:
        return None
    return user


async def create_session(
    session: AsyncSession,
    user: Profile,
    secret: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    settings = get_settings()
    key = new_session_key()
    now = datetime.now(timezone.utc)
    row = Session(
        key=key,
        user_id=user.id,
        auth_hash=session_auth_hash(user.password, secret),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(seconds=settings.session_max_age_seconds),
        ip_address=(ip or "")[:45] or None,
        user_agent=(user_agent or "")[:400] or None,
    )
    session.add(row)
    # Обновляем last_login — панель показывает его, и это ожидаемое поведение.
    await session.execute(
        update(Profile).where(Profile.id == user.id).values(last_login=now)
    )
    await session.commit()
    return key


async def resolve_user(session: AsyncSession, key: str | None, secret: str) -> Profile | None:
    """Достать пользователя по ключу сессии, проверив срок и отпечаток пароля."""
    if not key:
        return None
    row = (
        await session.execute(select(Session).where(Session.key == key))
    ).scalar_one_or_none()
    if row is None:
        return None

    now = datetime.now(timezone.utc)
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        await session.execute(delete(Session).where(Session.key == key))
        await session.commit()
        return None

    user = row.user
    if user is None or not user.can_login:
        return None
    # Смена пароля инвалидирует прежние сессии.
    if row.auth_hash != session_auth_hash(user.password, secret):
        await session.execute(delete(Session).where(Session.key == key))
        await session.commit()
        return None

    # Продлеваем «последнюю активность» не чаще раза в час, чтобы не писать в БД
    # на каждый запрос.
    last_seen = row.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if (now - last_seen) > timedelta(hours=1):
        await session.execute(
            update(Session).where(Session.key == key).values(last_seen_at=now)
        )
        await session.commit()
    return user


async def destroy_session(session: AsyncSession, key: str | None) -> None:
    if not key:
        return
    await session.execute(delete(Session).where(Session.key == key))
    await session.commit()
