"""Вход и выход."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.core.config import get_settings
from cbmail.core.security import CSRF_FIELD_NAME, validate_csrf_token
from cbmail.web import auth as auth_service
from cbmail.web.deps import get_current_user, get_db, redirect
from cbmail.web.templating import render

router = APIRouter()


def _safe_next(target: str | None) -> str:
    # Открытый редирект недопустим: принимаем только внутренние пути.
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/campaigns/"


@router.get("/login")
async def login_page(request: Request, user=Depends(get_current_user)):
    if user is not None:
        return RedirectResponse("/campaigns/", status_code=303)
    return render(request, "login.html", {"next": request.query_params.get("next", "")})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    seed = request.cookies.get("cbmail_csrf", "")
    form = await request.form()
    if not validate_csrf_token(str(form.get(CSRF_FIELD_NAME, "")), seed, request.app.state.secret_key):
        return render(
            request,
            "login.html",
            {"next": next, "error": "Сессия формы устарела, попробуйте ещё раз."},
            status_code=400,
        )

    user = await auth_service.authenticate(db, username, password)
    if user is None:
        return render(
            request,
            "login.html",
            {"next": next, "error": "Неверный логин или пароль.", "username": username},
            status_code=401,
        )

    key = await auth_service.create_session(
        db,
        user,
        request.app.state.secret_key,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    settings = get_settings()
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(
        settings.session_cookie_name,
        key,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
    )
    return resp


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    key = request.cookies.get(settings.session_cookie_name)
    await auth_service.destroy_session(db, key)
    resp = redirect("/login", ok="Вы вышли из системы.")
    resp.delete_cookie(settings.session_cookie_name)
    return resp
