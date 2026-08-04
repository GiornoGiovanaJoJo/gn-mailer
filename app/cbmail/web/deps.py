"""Зависимости FastAPI: БД-сессия, текущий пользователь, CSRF, flash-сообщения."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.core.config import get_settings
from cbmail.core.security import CSRF_FIELD_NAME, validate_csrf_token
from cbmail.db.models import Profile
from cbmail.db.session import get_async_session_factory
from cbmail.web import auth

FLASH_COOKIE = "cbmail_flash"


async def get_db() -> AsyncIterator[AsyncSession]:
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Profile | None:
    settings = get_settings()
    key = request.cookies.get(settings.session_cookie_name)
    user = await auth.resolve_user(db, key, request.app.state.secret_key)
    request.state.user = user
    return user


async def require_user(
    request: Request, user: Profile | None = Depends(get_current_user)
) -> Profile:
    if user is None:
        # Сохраняем адрес, куда пользователь шёл, чтобы вернуть после входа.
        target = request.url.path
        raise _RedirectException(f"/login?next={target}")
    return user


async def require_superuser(
    request: Request, user: Profile = Depends(require_user)
) -> Profile:
    if not user.is_superuser:
        raise _RedirectException("/campaigns/")
    return user


class _RedirectException(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


async def verify_csrf(request: Request) -> None:
    """Проверить CSRF-токен POST-запроса.

    Токен привязан к сид-значению из cookie ``cbmail_csrf`` (double-submit с
    подписью): украденный или подсунутый через поддомен токен к чужому сиду не
    подойдёт. Форма кешируется Starlette, поэтому обработчик прочитает её ещё раз
    без потерь.
    """
    seed = request.cookies.get("cbmail_csrf", "")
    form = await request.form()
    token = form.get(CSRF_FIELD_NAME, "")
    if not validate_csrf_token(str(token), seed, request.app.state.secret_key):
        raise _RedirectException("/login?err=csrf")


# --- Flash-сообщения ---------------------------------------------------------


def set_flash(response, *, ok: str | None = None, err: str | None = None) -> None:
    # Кодируем в base64: cookie передаётся заголовком latin-1, а сообщения
    # кириллические — сырой JSON уронил бы set_cookie на UnicodeEncodeError.
    raw = json.dumps({"ok": ok, "err": err}, ensure_ascii=False).encode("utf-8")
    payload = base64.urlsafe_b64encode(raw).decode("ascii")
    response.set_cookie(FLASH_COOKIE, payload, max_age=10, httponly=True, samesite="lax")


def redirect(url: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if ok or err:
        set_flash(resp, ok=ok, err=err)
    return resp


def read_flash(request: Request) -> dict | None:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
