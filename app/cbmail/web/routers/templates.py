"""Шаблоны писем: список/создание/редактирование/копирование/удаление.

Редактирование — через визуальный конструктор (emailbuilder), встроенный
iframe'ом. Обмен с ним идёт через postMessage (см. шаблоны form.html): при
сохранении конструктор отдаёт HTML (`mask`) и JSON-документ (`mask_json`),
при открытии существующего — получает `mask_json` обратно и восстанавливает вид.

Шаблоны общие для всех пользователей — в схеме у ``mail_messagetemplates`` нет
владельца, и так было в Django-версии; менять схему нельзя.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.core.config import get_settings
from cbmail.db.models import MessageTemplate, Profile
from cbmail.web.deps import get_db, redirect, require_user, verify_csrf
from cbmail.web.pagination import Page, offset, parse_page
from cbmail.web.templating import render

router = APIRouter(prefix="/templates")

_ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_PER_PAGE = 25


async def _get(db: AsyncSession, template_id: int) -> MessageTemplate | None:
    return (
        await db.execute(select(MessageTemplate).where(MessageTemplate.id == template_id))
    ).scalar_one_or_none()


@router.get("/")
async def list_templates(
    request: Request, user: Profile = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    page_no = parse_page(request.query_params.get("page"))
    total = (await db.execute(select(func.count()).select_from(MessageTemplate))).scalar() or 0
    rows = await db.execute(
        select(MessageTemplate)
        .order_by(MessageTemplate.id.desc())
        .offset(offset(page_no, _PER_PAGE))
        .limit(_PER_PAGE)
    )
    page = Page(items=list(rows.scalars()), page=page_no, per_page=_PER_PAGE, total=total)
    return render(request, "templates/list.html", {"page": page})


@router.get("/new")
async def new_template(request: Request, user: Profile = Depends(require_user)):
    return render(
        request,
        "templates/form.html",
        {"template": None, "action": "/templates/", "title": "Создание шаблона"},
    )


@router.post("/", dependencies=[Depends(verify_csrf)])
async def create_template(
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    name: str = Form(""),
    mask: str = Form(""),
    mask_json: str = Form(""),
):
    template = MessageTemplate(name=name.strip() or "Без названия", mask=mask, mask_json=mask_json)
    db.add(template)
    await db.commit()
    return redirect("/templates/", ok="Шаблон создан.")


@router.get("/{template_id}")
async def template_detail(
    template_id: int,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    template = await _get(db, template_id)
    if template is None:
        return redirect("/templates/", err="Шаблон не найден.")
    return render(request, "templates/detail.html", {"template": template})


@router.get("/{template_id}/content")
async def template_content(
    template_id: int,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """HTML и JSON шаблона — для подстановки в кампанию и загрузки в редактор."""
    template = await _get(db, template_id)
    if template is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(
        {"html": template.mask or "", "mask_json": template.mask_json or "", "name": template.name or ""}
    )


@router.get("/{template_id}/edit")
async def edit_template(
    template_id: int,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    template = await _get(db, template_id)
    if template is None:
        return redirect("/templates/", err="Шаблон не найден.")
    return render(
        request,
        "templates/form.html",
        {"template": template, "action": f"/templates/{template_id}/edit", "title": "Редактирование шаблона"},
    )


@router.post("/{template_id}/edit", dependencies=[Depends(verify_csrf)])
async def update_template(
    template_id: int,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    name: str = Form(""),
    mask: str = Form(""),
    mask_json: str = Form(""),
):
    template = await _get(db, template_id)
    if template is None:
        return redirect("/templates/", err="Шаблон не найден.")
    template.name = name.strip() or "Без названия"
    template.mask = mask
    template.mask_json = mask_json
    await db.commit()
    return redirect("/templates/", ok="Шаблон обновлён.")


@router.post("/{template_id}/copy", dependencies=[Depends(verify_csrf)])
async def copy_template(
    template_id: int,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    template = await _get(db, template_id)
    if template is None:
        return redirect("/templates/", err="Шаблон не найден.")
    copy = MessageTemplate(
        name=f"{template.name or 'Без названия'} (копия)",
        mask=template.mask,
        mask_json=template.mask_json,
    )
    db.add(copy)
    await db.commit()
    return redirect("/templates/", ok="Шаблон скопирован.")


@router.post("/{template_id}/delete", dependencies=[Depends(verify_csrf)])
async def delete_template(
    template_id: int,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(delete(MessageTemplate).where(MessageTemplate.id == template_id))
    await db.commit()
    return redirect("/templates/", ok="Шаблон удалён.")


# --- Загрузка картинок для конструктора --------------------------------------
# Конструктор (emailbuilder) шлёт multipart с полем `image` на этот адрес и ждёт
# ответ {success: true, url: "..."}. Путь ЖЁСТКО задан в собранном JS редактора
# (`/mail/api/upload-image/`), поэтому эндпоинт висит именно на нём. CSRF здесь
# не проверяем: запрос идёт из iframe того же origin под сессионной cookie, а
# токен редактор передаёт своим заголовком не всегда.

image_router = APIRouter()


@image_router.post("/mail/api/upload-image/")
async def upload_image(
    user: Profile = Depends(require_user),
    image: UploadFile = File(...),
):
    ext = Path(image.filename or "").suffix.lower()
    if ext not in _ALLOWED_IMAGE_EXT:
        return JSONResponse({"success": False, "error": "Недопустимый тип файла"}, status_code=400)
    settings = get_settings()
    content = await image.read()
    if len(content) > settings.smtp_attachment_max_bytes:
        return JSONResponse({"success": False, "error": "Файл слишком большой"}, status_code=400)
    dest_dir = Path(settings.media_root) / "email_images"
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (dest_dir / name).write_bytes(content)
    return JSONResponse({"success": True, "url": f"/media/email_images/{name}"})
