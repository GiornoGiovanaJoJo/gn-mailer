"""Управление пользователями (только суперпользователь).

Список, создание, редактирование, блокировка/разблокировка и мягкое удаление.
Работает с той же моделью ``useraccount.Profile``; пароли пишутся в Django-совместимом
формате, поэтому созданные здесь учётки работают и в старой панели.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.core.security import hash_password
from cbmail.db.models import Profile
from cbmail.web.deps import get_db, redirect, require_superuser, verify_csrf
from cbmail.web.pagination import Page, offset, parse_page
from cbmail.web.templating import render

router = APIRouter(prefix="/users")

_PER_PAGE = 25


def _bool(value: str) -> bool:
    return str(value).lower() in {"1", "true", "on", "yes"}


async def _get(db: AsyncSession, user_id: uuid.UUID) -> Profile | None:
    return (
        await db.execute(select(Profile).where(Profile.id == user_id))
    ).scalar_one_or_none()


@router.get("/")
async def list_users(
    request: Request,
    admin: Profile = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
):
    page_no = parse_page(request.query_params.get("page"))
    q = (request.query_params.get("q") or "").strip()
    where = [Profile.deleted.is_(False)]
    if q:
        like = f"%{q}%"
        where.append(or_(Profile.username.ilike(like), Profile.email.ilike(like)))

    total = (
        await db.execute(select(func.count()).select_from(Profile).where(*where))
    ).scalar() or 0
    rows = await db.execute(
        select(Profile)
        .where(*where)
        .order_by(Profile.date_joined.desc())
        .offset(offset(page_no, _PER_PAGE))
        .limit(_PER_PAGE)
    )
    page = Page(items=list(rows.scalars()), page=page_no, per_page=_PER_PAGE, total=total)
    return render(request, "users/list.html", {"page": page, "q": q})


@router.get("/new")
async def new_user(request: Request, admin: Profile = Depends(require_superuser)):
    return render(request, "users/form.html", {"profile": None, "action": "/users/"})


@router.post("/", dependencies=[Depends(verify_csrf)])
async def create_user(
    request: Request,
    admin: Profile = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
    username: str = Form(""),
    password: str = Form(""),
    email: str = Form(""),
    first_name: str = Form(""),
    last_name: str = Form(""),
    is_staff: str = Form(""),
    is_superuser: str = Form(""),
):
    username = username.strip()
    if not username or not password:
        return render(
            request,
            "users/form.html",
            {"profile": None, "action": "/users/", "error": "Укажите логин и пароль."},
            status_code=400,
        )
    profile = Profile(
        id=uuid.uuid4(),
        password=hash_password(password),
        username=username,
        email=email.strip(),
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        is_active=True,
        is_staff=_bool(is_staff) or _bool(is_superuser),
        is_superuser=_bool(is_superuser),
        type=1,
        gender=1,
        avatar="default/user-nophoto.png",
        online=False,
        blocked=False,
        deleted=False,
    )
    db.add(profile)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return render(
            request,
            "users/form.html",
            {"profile": None, "action": "/users/", "error": "Логин уже занят."},
            status_code=400,
        )
    return redirect("/users/", ok="Пользователь создан.")


@router.get("/{user_id}/edit")
async def edit_user(
    user_id: uuid.UUID,
    request: Request,
    admin: Profile = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
):
    profile = await _get(db, user_id)
    if profile is None:
        return redirect("/users/", err="Пользователь не найден.")
    return render(
        request, "users/form.html", {"profile": profile, "action": f"/users/{user_id}/edit"}
    )


@router.post("/{user_id}/edit", dependencies=[Depends(verify_csrf)])
async def update_user(
    user_id: uuid.UUID,
    request: Request,
    admin: Profile = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
    password: str = Form(""),
    email: str = Form(""),
    first_name: str = Form(""),
    last_name: str = Form(""),
    is_staff: str = Form(""),
    is_superuser: str = Form(""),
):
    profile = await _get(db, user_id)
    if profile is None:
        return redirect("/users/", err="Пользователь не найден.")
    profile.email = email.strip()
    profile.first_name = first_name.strip()
    profile.last_name = last_name.strip()
    profile.is_superuser = _bool(is_superuser)
    profile.is_staff = _bool(is_staff) or profile.is_superuser
    if password:
        profile.password = hash_password(password)
    await db.commit()
    return redirect("/users/", ok="Пользователь обновлён.")


@router.post("/{user_id}/block", dependencies=[Depends(verify_csrf)])
async def block_user(
    user_id: uuid.UUID,
    admin: Profile = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
):
    profile = await _get(db, user_id)
    if profile is None:
        return redirect("/users/", err="Пользователь не найден.")
    if profile.id == admin.id:
        return redirect("/users/", err="Нельзя заблокировать самого себя.")
    profile.blocked = True
    await db.commit()
    return redirect("/users/", ok=f"Пользователь «{profile.username}» заблокирован.")


@router.post("/{user_id}/unblock", dependencies=[Depends(verify_csrf)])
async def unblock_user(
    user_id: uuid.UUID,
    admin: Profile = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
):
    profile = await _get(db, user_id)
    if profile is None:
        return redirect("/users/", err="Пользователь не найден.")
    profile.blocked = False
    await db.commit()
    return redirect("/users/", ok=f"Пользователь «{profile.username}» разблокирован.")


@router.post("/{user_id}/delete", dependencies=[Depends(verify_csrf)])
async def delete_user(
    user_id: uuid.UUID,
    admin: Profile = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
):
    profile = await _get(db, user_id)
    if profile is None:
        return redirect("/users/", err="Пользователь не найден.")
    if profile.id == admin.id:
        return redirect("/users/", err="Нельзя удалить самого себя.")
    # Мягкое удаление — как в старой панели (флаг deleted), данные сохраняются.
    profile.deleted = True
    profile.is_active = False
    await db.commit()
    return redirect("/users/", ok=f"Пользователь «{profile.username}» удалён.")
