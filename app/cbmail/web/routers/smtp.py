"""Профили SMTP (мульти-SMTP): по профилю на проект/домен, выбор в кампании."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.core.security import CSRF_HEADER_NAME, validate_csrf_token
from cbmail.db.models import MailProject, Profile, SmtpCheckLog, SmtpProfile
from cbmail.smtp.sender import SmtpCredentials, verify_credentials_async
from cbmail.web.deps import get_db, redirect, require_user, verify_csrf
from cbmail.web.templating import render

router = APIRouter(prefix="/smtp")


def _bool(value: str) -> bool:
    return str(value).lower() in {"1", "true", "on", "yes"}


def _int(value: str, default: int = 0) -> int:
    try:
        return max(0, int(float(str(value).strip().replace(",", "."))))
    except (TypeError, ValueError):
        return default


async def _owned(db: AsyncSession, profile_id: int, user: Profile) -> SmtpProfile | None:
    row = await db.execute(
        select(SmtpProfile).where(SmtpProfile.id == profile_id, SmtpProfile.user_id == user.id)
    )
    return row.scalar_one_or_none()


async def _projects(db: AsyncSession, user: Profile) -> list[MailProject]:
    rows = await db.execute(
        select(MailProject).where(MailProject.user_id == user.id).order_by(MailProject.name)
    )
    return list(rows.scalars())


async def _resolve_project(
    db: AsyncSession, user: Profile, project_id: str, new_project: str
) -> int | None:
    """Определить проект для профиля: новый по названию, существующий по id, либо ничего."""
    name = new_project.strip()
    if name:
        existing = await db.execute(
            select(MailProject).where(MailProject.user_id == user.id, MailProject.name == name)
        )
        project = existing.scalar_one_or_none()
        if project is None:
            project = MailProject(name=name, user_id=user.id)
            db.add(project)
            await db.flush()
        return project.id
    if project_id.strip():
        owned = await db.execute(
            select(MailProject.id).where(
                MailProject.id == _int(project_id), MailProject.user_id == user.id
            )
        )
        return owned.scalar_one_or_none()
    return None


@router.get("/")
async def list_profiles(
    request: Request, user: Profile = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    rows = await db.execute(
        select(SmtpProfile).where(SmtpProfile.user_id == user.id).order_by(SmtpProfile.id)
    )
    return render(request, "smtp/list.html", {
        "profiles": list(rows.scalars()), "edit": None,
        "projects": await _projects(db, user),
    })


@router.get("/{profile_id}/edit")
async def edit_profile(
    profile_id: int,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    profile = await _owned(db, profile_id, user)
    if profile is None:
        return redirect("/smtp/", err="Профиль не найден.")
    rows = await db.execute(
        select(SmtpProfile).where(SmtpProfile.user_id == user.id).order_by(SmtpProfile.id)
    )
    return render(request, "smtp/list.html", {
        "profiles": list(rows.scalars()), "edit": profile,
        "projects": await _projects(db, user),
    })


@router.post("/", dependencies=[Depends(verify_csrf)])
async def create_profile(
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    title: str = Form(""),
    email_host: str = Form(""),
    email_port: str = Form("587"),
    email_host_user: str = Form(""),
    email_host_password: str = Form(""),
    default_from_email: str = Form(""),
    email_use_tls: str = Form(""),
    email_use_ssl: str = Form(""),
    message_header: str = Form(""),
    message_footer: str = Form(""),
    daily_limit: str = Form("0"),
    project_id: str = Form(""),
    new_project: str = Form(""),
):
    project = await _resolve_project(db, user, project_id, new_project)
    profile = SmtpProfile(
        title=title.strip() or "Профиль",
        email_host=email_host.strip(),
        email_port=email_port.strip() or "587",
        email_host_user=email_host_user.strip(),
        email_host_password=email_host_password,
        default_from_email=default_from_email.strip() or email_host_user.strip(),
        email_use_tls=_bool(email_use_tls),
        email_use_ssl=_bool(email_use_ssl),
        message_header=message_header,
        message_footer=message_footer,
        daily_limit=_int(daily_limit),
        project_id=project,
        user_id=user.id,
    )
    db.add(profile)
    await db.commit()
    return redirect("/smtp/", ok="Профиль SMTP создан.")


@router.post("/{profile_id}/edit", dependencies=[Depends(verify_csrf)])
async def update_profile(
    profile_id: int,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    title: str = Form(""),
    email_host: str = Form(""),
    email_port: str = Form("587"),
    email_host_user: str = Form(""),
    email_host_password: str = Form(""),
    default_from_email: str = Form(""),
    email_use_tls: str = Form(""),
    email_use_ssl: str = Form(""),
    message_header: str = Form(""),
    message_footer: str = Form(""),
    daily_limit: str = Form("0"),
    project_id: str = Form(""),
    new_project: str = Form(""),
):
    profile = await _owned(db, profile_id, user)
    if profile is None:
        return redirect("/smtp/", err="Профиль не найден.")
    profile.title = title.strip() or "Профиль"
    profile.email_host = email_host.strip()
    profile.email_port = email_port.strip() or "587"
    profile.email_host_user = email_host_user.strip()
    # Пустой пароль в форме означает «не менять» — иначе редактирование прочих
    # полей молча стирало бы сохранённый пароль.
    if email_host_password:
        profile.email_host_password = email_host_password
    profile.default_from_email = default_from_email.strip() or email_host_user.strip()
    profile.email_use_tls = _bool(email_use_tls)
    profile.email_use_ssl = _bool(email_use_ssl)
    profile.message_header = message_header
    profile.message_footer = message_footer
    profile.daily_limit = _int(daily_limit)
    profile.project_id = await _resolve_project(db, user, project_id, new_project)
    await db.commit()
    return redirect("/smtp/", ok="Профиль обновлён.")


@router.post("/{profile_id}/delete", dependencies=[Depends(verify_csrf)])
async def delete_profile(
    profile_id: int,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    profile = await _owned(db, profile_id, user)
    if profile is None:
        return redirect("/smtp/", err="Профиль не найден.")
    await db.execute(delete(SmtpProfile).where(SmtpProfile.id == profile_id))
    await db.commit()
    return redirect("/smtp/", ok="Профиль удалён.")


@router.post("/projects", dependencies=[Depends(verify_csrf)])
async def create_project(
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    project_name: str = Form(""),
):
    name = project_name.strip()
    if not name:
        return redirect("/smtp/", err="Укажите название проекта.")
    exists = await db.execute(
        select(MailProject).where(MailProject.user_id == user.id, MailProject.name == name)
    )
    if exists.scalar_one_or_none() is None:
        db.add(MailProject(name=name, user_id=user.id))
        await db.commit()
    return redirect("/smtp/", ok=f"Проект «{name}» создан.")


@router.post("/projects/{project_id}/delete", dependencies=[Depends(verify_csrf)])
async def delete_project(
    project_id: int,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    owned = await db.execute(
        select(MailProject).where(MailProject.id == project_id, MailProject.user_id == user.id)
    )
    project = owned.scalar_one_or_none()
    if project is None:
        return redirect("/smtp/", err="Проект не найден.")
    # Открепляем почты (project_id → NULL), сами профили не трогаем.
    await db.execute(
        update(SmtpProfile).where(SmtpProfile.project_id == project_id).values(project_id=None)
    )
    await db.execute(delete(MailProject).where(MailProject.id == project_id))
    await db.commit()
    return redirect("/smtp/", ok=f"Проект «{project.name}» удалён (почты сохранены).")


@router.post("/test-connection")
async def test_connection_form(request: Request, user: Profile = Depends(require_user)):
    """Проверка подключения по НЕСОХРАНЁННЫМ значениям формы (AJAX из шаблона).

    CSRF берём из заголовка ``X-CSRFToken`` (его шлёт JS) или из поля формы.
    """
    form = await request.form()
    seed = request.cookies.get("cbmail_csrf", "")
    token = request.headers.get(CSRF_HEADER_NAME) or form.get("csrfmiddlewaretoken", "")
    if not validate_csrf_token(str(token), seed, request.app.state.secret_key):
        return JSONResponse({"status": "error", "message": "Сессия формы устарела."}, status_code=400)

    profile = SimpleNamespace(
        email_host=str(form.get("email_host", "")),
        email_port=str(form.get("email_port", "")),
        email_host_user=str(form.get("email_host_user", "")),
        email_host_password=str(form.get("email_host_password", "")),
        email_use_tls=str(form.get("email_use_tls", "")).lower() in {"1", "true", "on", "yes"},
        email_use_ssl=str(form.get("email_use_ssl", "")).lower() in {"1", "true", "on", "yes"},
    )
    try:
        credentials = SmtpCredentials.from_profile(profile)
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)})
    ok, message = await verify_credentials_async(credentials)
    return JSONResponse({"status": "success" if ok else "error", "message": message})


@router.post("/{profile_id}/test", dependencies=[Depends(verify_csrf)])
async def test_profile(
    profile_id: int,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    profile = await _owned(db, profile_id, user)
    if profile is None:
        return redirect("/smtp/", err="Профиль не найден.")
    try:
        credentials = SmtpCredentials.from_profile(profile)
    except Exception as exc:
        ok, message = False, str(exc)
    else:
        ok, message = await verify_credentials_async(credentials)

    now = datetime.now(timezone.utc)
    profile.last_check_time = now
    profile.last_check_success = ok
    profile.last_error = None if ok else message[:2000]
    profile.check_count = (profile.check_count or 0) + 1
    if ok:
        profile.success_count = (profile.success_count or 0) + 1
    else:
        profile.failure_count = (profile.failure_count or 0) + 1
    db.add(
        SmtpCheckLog(
            smtp_settings_id=profile_id,
            status="success" if ok else "failure",
            message=message[:2000],
        )
    )
    await db.commit()
    if ok:
        return redirect("/smtp/", ok=f"«{profile.title}»: подключение успешно.")
    return redirect("/smtp/", err=f"«{profile.title}»: {message}")
