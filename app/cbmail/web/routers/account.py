"""Личный кабинет пользователя: профиль и безопасность (смена пароля)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.core.security import hash_password, verify_password
from cbmail.db.models import Profile
from cbmail.web.deps import get_db, redirect, require_user, verify_csrf
from cbmail.web.templating import render

router = APIRouter(prefix="/account")


@router.get("/profile")
async def profile_page(request: Request, user: Profile = Depends(require_user)):
    return render(request, "account/profile.html", {})


@router.post("/profile", dependencies=[Depends(verify_csrf)])
async def profile_save(
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(""),
):
    user.first_name = first_name.strip()
    user.last_name = last_name.strip()
    user.email = email.strip()
    await db.commit()
    return redirect("/account/profile", ok="Профиль обновлён.")


@router.get("/security")
async def security_page(request: Request, user: Profile = Depends(require_user)):
    return render(request, "account/security.html", {})


@router.post("/security", dependencies=[Depends(verify_csrf)])
async def security_save(
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    error: str | None = None
    try:
        if not verify_password(current_password, user.password):
            error = "Текущий пароль неверен."
    except Exception:
        error = "Текущий пароль неверен."
    if error is None and len(new_password) < 6:
        error = "Новый пароль слишком короткий (минимум 6 символов)."
    if error is None and new_password != confirm_password:
        error = "Пароли не совпадают."
    if error:
        return render(request, "account/security.html", {"error": error}, status_code=400)

    user.password = hash_password(new_password)
    await db.commit()
    # После смены пароля прежние сессии инвалидируются (auth_hash), кроме текущей —
    # но текущая сессия хранит старый auth_hash, поэтому её тоже разлогинит.
    resp = redirect("/login", ok="Пароль изменён. Войдите заново.")
    from cbmail.core.config import get_settings

    resp.delete_cookie(get_settings().session_cookie_name)
    return resp
