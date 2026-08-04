"""Публичные страницы без авторизации: отписка от рассылки.

Ссылка отписки вшивается в каждое письмо (заголовок ``List-Unsubscribe`` и
плейсхолдер ``{{unsubscribe_url}}``). Она открывается из почтового клиента — без
сессии и CSRF — и добавляет адрес в стоп-лист. Без рабочего адреса получатели
жмут «Спам», что портит репутацию домена отправителя.

Отписка адресная: параметр ``d`` несёт домен отправителя, и человек отписывается
именно от его писем, а не от всех рассылок сразу. Отсутствие ``d`` (старые
письма) означает глобальную отписку — обратная совместимость.

Адрес email передаётся через QUERY-параметр ``e`` (а не через path), потому что
символ ``@`` в URL-пути ненадёжен: nginx/прокси трактуют всё после ``@`` как
хост и обрезают/переписывают путь, что приводит к 400 при любом кодировании.
"""

from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.db.models import Unsubscribe
from cbmail.domain.emails import is_valid_email, normalize_email
from cbmail.web.deps import get_db
from cbmail.web.templating import render

router = APIRouter()


@router.get("/unsubscribe/{email:path}")
async def unsubscribe_path(email: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Обратная совместимость: старые письма содержат /unsubscribe/email@domain/."""
    return await _do_unsubscribe(unquote(email or ""), "", request, db)


@router.get("/unsubscribe")
async def unsubscribe_query(
    request: Request,
    e: str = Query(default="", alias="e"),
    d: str = Query(default="", alias="d"),
    db: AsyncSession = Depends(get_db),
):
    """Новые письма: /unsubscribe?e=email%40domain&d=sender-domain.ru."""
    return await _do_unsubscribe(unquote(e or ""), unquote(d or ""), request, db)


async def _do_unsubscribe(raw_email: str, raw_scope: str, request: Request, db: AsyncSession):
    email = normalize_email(raw_email)
    if not is_valid_email(email):
        return PlainTextResponse("Некорректный адрес", status_code=400)

    scope = (raw_scope or "").strip().lower()

    existing = (
        await db.execute(
            select(Unsubscribe).where(
                Unsubscribe.email == email, Unsubscribe.scope == scope
            )
        )
    ).scalar_one_or_none()

    already = existing is not None
    if not already:
        db.add(Unsubscribe(email=email, scope=scope))
        await db.commit()

    return render(
        request,
        "unsubscribed.html",
        {"email": email, "already": already, "scope": scope},
    )
