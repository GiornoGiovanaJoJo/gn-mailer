"""Письма (модуль «Сообщения»): чтение и действия над письмами.

Чтение: список своих писем (`/messages/`), общий список для суперпользователя
(`/all-messages/`), карточка письма. Действия: избранное (звезда), прочитано/
непрочитано, корзина/восстановление — состояние хранится в тех же таблицах, что
и в Django-версии (m2m ``read_by``/``starred_by`` и мягкое удаление через
``message_rm_id``). Создание/отправка письма и папки — отдельный слой.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.db.models import (
    Message,
    MessageDir,
    MessageRm,
    Profile,
    SmtpProfile,
)
from cbmail.domain import composer
from cbmail.domain.composer import looks_like_html
from cbmail.domain.emails import split_emails
from cbmail.smtp.sender import (
    OutgoingMessage,
    SmtpCredentials,
    SmtpError,
    SmtpSession,
    resolve_from_email,
)
from cbmail.web.deps import get_db, redirect, require_user, verify_csrf
from cbmail.web.pagination import Page, offset, parse_page
from cbmail.web.templating import render

router = APIRouter()

_PER_PAGE = 25
_TRASH_KEY = "trash"


async def _trash_marker(db: AsyncSession) -> MessageRm:
    """Единый маркер «в корзине», на который ссылаются удалённые письма.

    Схема хранит мягкое удаление через FK ``message_rm_id``; общий маркер даёт
    ту же наблюдаемую механику (скрыто из «Входящих», видно в «Корзине»,
    восстановимо), не заводя строку-причину на каждое письмо.
    """
    marker = (
        await db.execute(select(MessageRm).where(MessageRm.key == _TRASH_KEY).limit(1))
    ).scalar_one_or_none()
    if marker is None:
        marker = MessageRm(name="Корзина", key=_TRASH_KEY)
        db.add(marker)
        await db.flush()
    return marker


async def _owned(db: AsyncSession, message_id: uuid.UUID, user: Profile) -> Message | None:
    message = (
        await db.execute(select(Message).where(Message.id == message_id))
    ).scalar_one_or_none()
    if message is None:
        return None
    if message.user_id != user.id and not user.is_superuser:
        return None
    return message


async def _list(request: Request, db: AsyncSession, *, title: str, all_users: bool, owner: Profile):
    page = parse_page(request.query_params.get("page"))
    box = request.query_params.get("box", "inbox")
    dir_raw = request.query_params.get("dir", "")
    where = [] if all_users else [Message.user_id == owner.id]
    where.append(
        Message.message_rm_id.is_not(None) if box == "trash" else Message.message_rm_id.is_(None)
    )
    active_dir = int(dir_raw) if dir_raw.isdigit() else None
    if active_dir is not None:
        where.append(Message.dirs.any(MessageDir.id == active_dir))

    total = (await db.execute(select(func.count()).select_from(Message).where(*where))).scalar() or 0
    rows = await db.execute(
        select(Message)
        .where(*where)
        .order_by(Message.created_at.desc())
        .offset(offset(page, _PER_PAGE))
        .limit(_PER_PAGE)
    )
    items = list(rows.scalars())
    pageset = Page(items=items, page=page, per_page=_PER_PAGE, total=total)
    uid = owner.id
    return render(
        request,
        "messages/list.html",
        {
            "page": pageset,
            "title": title,
            "all_users": all_users,
            "box": box,
            "active_dir": active_dir,
            "folders": await _folders(db, owner),
            "read_map": {m.id: any(p.id == uid for p in m.read_by) for m in items},
            "star_map": {m.id: any(p.id == uid for p in m.starred_by) for m in items},
        },
    )


@router.get("/messages/")
async def my_messages(
    request: Request, user: Profile = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    box = request.query_params.get("box", "inbox")
    title = "Корзина" if box == "trash" else "Письма"
    return await _list(request, db, title=title, all_users=False, owner=user)


@router.get("/all-messages/")
async def all_messages(
    request: Request, user: Profile = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    if not user.is_superuser:
        return redirect("/messages/", err="Раздел «Все письма» доступен только администратору.")
    return await _list(request, db, title="Все письма", all_users=True, owner=user)


async def _folders(db: AsyncSession, user: Profile) -> list[MessageDir]:
    rows = await db.execute(
        select(MessageDir).where(MessageDir.user_id == user.id).order_by(MessageDir.name)
    )
    return list(rows.scalars())


async def _smtp_profiles(db: AsyncSession, user: Profile) -> list[SmtpProfile]:
    rows = await db.execute(
        select(SmtpProfile).where(SmtpProfile.user_id == user.id).order_by(SmtpProfile.id)
    )
    return list(rows.scalars())


def _send_single(profile: SmtpProfile, recipients: list[str], subject: str, body: str) -> None:
    """Отправить одно письмо на список адресов (синхронно, для to_thread)."""
    credentials = SmtpCredentials.from_profile(profile)
    composed = composer.compose(
        subject=subject, body=body,
        header=profile.message_header, footer=profile.message_footer,
    )
    from_email = resolve_from_email(credentials, profile.default_from_email)
    with SmtpSession(credentials) as smtp:
        for addr in recipients:
            personal = composed.personalize(addr)
            outgoing = OutgoingMessage(
                subject=personal.subject, text_body=personal.text_body,
                html_body=personal.html_body, from_email=from_email,
            )
            smtp.send(outgoing, addr)


@router.get("/messages/new")
async def compose_message(
    request: Request, user: Profile = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    return render(
        request, "messages/compose.html", {"profiles": await _smtp_profiles(db, user)}
    )


@router.post("/messages/new", dependencies=[Depends(verify_csrf)])
async def send_message(
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    clients: str = Form(""),
    subject: str = Form(""),
    message: str = Form(""),
    use_smtp_settings_id: str = Form(""),
):
    valid, invalid = split_emails(clients)
    if not valid:
        return render(
            request,
            "messages/compose.html",
            {"profiles": await _smtp_profiles(db, user), "error": "Укажите хотя бы один корректный адрес."},
            status_code=400,
        )
    if not subject.strip() or not message.strip():
        return render(
            request,
            "messages/compose.html",
            {"profiles": await _smtp_profiles(db, user), "error": "Заполните тему и текст письма."},
            status_code=400,
        )

    profiles = await _smtp_profiles(db, user)
    profile = None
    if use_smtp_settings_id.strip().isdigit():
        profile = next((p for p in profiles if p.id == int(use_smtp_settings_id)), None)
    profile = profile or (profiles[0] if profiles else None)
    if profile is None:
        return render(
            request,
            "messages/compose.html",
            {"profiles": profiles, "error": "Нет ни одного профиля SMTP — добавьте его в «Настройках»."},
            status_code=400,
        )

    try:
        await asyncio.to_thread(_send_single, profile, valid, subject.strip(), message)
    except SmtpError as exc:
        return render(
            request,
            "messages/compose.html",
            {"profiles": profiles, "error": f"Не удалось отправить: {exc}"},
            status_code=400,
        )

    db.add(
        Message(
            id=uuid.uuid4(),
            custom_id="M" + secrets.token_hex(8),
            user_id=user.id,
            clients=", ".join(valid),
            message=message,
            subject=subject.strip(),
            message_type="outgoing",
            self_field=False,
        )
    )
    await db.commit()
    note = f"Письмо отправлено ({len(valid)} получат.)."
    if invalid:
        note += f" Не приняты как адреса: {len(invalid)}."
    return redirect("/messages/", ok=note)


@router.post("/messages/folders", dependencies=[Depends(verify_csrf)])
async def create_folder(
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    name: str = Form(""),
):
    name = name.strip()
    if not name:
        return redirect("/messages/", err="Укажите название папки.")
    db.add(MessageDir(name=name, user_id=user.id))
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        return redirect("/messages/", err="Папка с таким названием уже есть.")
    return redirect("/messages/", ok=f"Папка «{name}» создана.")


@router.post("/messages/{message_id}/move", dependencies=[Depends(verify_csrf)])
async def move_message(
    message_id: uuid.UUID,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    dir_id: str = Form(""),
):
    message = await _owned(db, message_id, user)
    if message is None:
        return redirect("/messages/", err="Письмо не найдено.")
    if dir_id.strip().isdigit():
        folder = (
            await db.execute(
                select(MessageDir).where(
                    MessageDir.id == int(dir_id), MessageDir.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        message.dirs = [folder] if folder else []
    else:
        message.dirs = []
    await db.commit()
    return redirect("/messages/", ok="Письмо перемещено.")


@router.get("/messages/{message_id}")
async def message_detail(
    message_id: uuid.UUID,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    message = await _owned(db, message_id, user)
    if message is None:
        return redirect("/messages/", err="Письмо не найдено или нет доступа.")
    return render(
        request,
        "messages/detail.html",
        {
            "message": message,
            "is_html": looks_like_html(message.message),
            "is_read": any(p.id == user.id for p in message.read_by),
            "is_starred": any(p.id == user.id for p in message.starred_by),
        },
    )


# --- Действия над письмом ----------------------------------------------------


def _back(request: Request, message_id: uuid.UUID) -> str:
    ref = request.headers.get("referer", "")
    return ref if "/messages" in ref else f"/messages/{message_id}"


@router.post("/messages/{message_id}/star", dependencies=[Depends(verify_csrf)])
async def toggle_star(
    message_id: uuid.UUID,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    message = await _owned(db, message_id, user)
    if message is None:
        return redirect("/messages/", err="Письмо не найдено.")
    if any(p.id == user.id for p in message.starred_by):
        message.starred_by = [p for p in message.starred_by if p.id != user.id]
        note = "Убрано из избранного."
    else:
        message.starred_by.append(user)
        note = "Добавлено в избранное."
    await db.commit()
    return redirect(_back(request, message_id), ok=note)


@router.post("/messages/{message_id}/read", dependencies=[Depends(verify_csrf)])
async def toggle_read(
    message_id: uuid.UUID,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    message = await _owned(db, message_id, user)
    if message is None:
        return redirect("/messages/", err="Письмо не найдено.")
    if any(p.id == user.id for p in message.read_by):
        message.read_by = [p for p in message.read_by if p.id != user.id]
        note = "Отмечено как непрочитанное."
    else:
        message.read_by.append(user)
        note = "Отмечено как прочитанное."
    await db.commit()
    return redirect(_back(request, message_id), ok=note)


@router.post("/messages/{message_id}/trash", dependencies=[Depends(verify_csrf)])
async def trash_message(
    message_id: uuid.UUID,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    message = await _owned(db, message_id, user)
    if message is None:
        return redirect("/messages/", err="Письмо не найдено.")
    marker = await _trash_marker(db)
    message.message_rm_id = marker.id
    await db.commit()
    return redirect("/messages/", ok="Письмо перемещено в корзину.")


@router.post("/messages/{message_id}/restore", dependencies=[Depends(verify_csrf)])
async def restore_message(
    message_id: uuid.UUID,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    message = await _owned(db, message_id, user)
    if message is None:
        return redirect("/messages/", err="Письмо не найдено.")
    message.message_rm_id = None
    await db.commit()
    return redirect("/messages/?box=trash", ok="Письмо восстановлено.")
