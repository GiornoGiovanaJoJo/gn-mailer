"""Кампании массовой рассылки: список, создание, редактирование, СТОП, удаление.

«Стоп» — это перевод в статус ``cancelled`` без удаления: воркер проверяет
статус перед каждым письмом и пачкой и прекращает отправку, а кампания
остаётся в списке с накопленной статистикой. Именно этого просил клиент —
остановить идущую рассылку, не теряя её.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.core.config import get_settings
from cbmail.db.models import (
    Campaign,
    CampaignFile,
    CampaignLog,
    MessageTemplate,
    Profile,
    SmtpProfile,
)
from cbmail.domain import composer
from cbmail.domain.campaign import CampaignStatus, clamp_batch_size, clamp_interval
from cbmail.domain.emails import join_emails, parse_emails, split_emails
from cbmail.web.deps import get_db, redirect, require_user, verify_csrf
from cbmail.web.pagination import Page, offset, parse_page
from cbmail.web.templating import render

router = APIRouter(prefix="/campaigns")

_MAX_ATTACHMENTS = 3
_PER_PAGE = 25
_STOPPABLE = {CampaignStatus.SENDING.value, CampaignStatus.SCHEDULED.value}


async def _user_profiles(db: AsyncSession, user: Profile) -> list[SmtpProfile]:
    rows = await db.execute(
        select(SmtpProfile).where(SmtpProfile.user_id == user.id).order_by(SmtpProfile.id)
    )
    return list(rows.scalars())


async def _templates(db: AsyncSession) -> list[MessageTemplate]:
    rows = await db.execute(select(MessageTemplate).order_by(MessageTemplate.id.desc()))
    return list(rows.scalars())


async def _resolve_message(db: AsyncSession, message: str, template_id: str) -> str:
    """Собрать тело письма из выбранного шаблона и/или собственного текста.

    Поддерживаются все три сочетания, как просил пользователь:

    * только шаблон (поле письма пустое) — тело берётся из шаблона;
    * только текст (шаблон не выбран) — тело как есть;
    * и то и другое — текст пользователя сверху, оформление шаблона ниже.

    Раньше при выбранном шаблоне и пустом поле выдавалось «Письмо не может быть
    пустым», а при заполненном поле шаблон молча игнорировался. Объединение идёт
    на сервере и не зависит от JS.
    """
    tid = _int_or_none(template_id)
    if tid is None:
        return message
    template = (
        await db.execute(select(MessageTemplate).where(MessageTemplate.id == tid))
    ).scalar_one_or_none()
    if template is None:
        return message
    return composer.combine_body(template.mask, message)


async def _get_owned(db: AsyncSession, campaign_id: int, user: Profile) -> Campaign | None:
    row = await db.execute(
        select(Campaign).where(Campaign.id == campaign_id, Campaign.user_id == user.id)
    )
    return row.scalar_one_or_none()


def _parse_schedule(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    tz = ZoneInfo(get_settings().timezone)
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


async def _save_attachments(
    db: AsyncSession, campaign: Campaign, files: list[UploadFile], existing: int
) -> tuple[int, list[str]]:
    settings = get_settings()
    warnings: list[str] = []
    allowed = max(0, _MAX_ATTACHMENTS - existing)
    saved = 0
    incoming = [f for f in files if f and f.filename]
    if len(incoming) > allowed:
        warnings.append(f"Можно прикрепить не более {_MAX_ATTACHMENTS} файлов — лишние пропущены.")
    dest_dir = Path(settings.media_root) / "mass_mail" / str(campaign.id)
    for upload in incoming[:allowed]:
        content = await upload.read()
        if len(content) > settings.smtp_attachment_max_bytes:
            warnings.append(f"Файл «{upload.filename}» больше 10 МБ — пропущен.")
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(upload.filename).name
        (dest_dir / safe_name).write_bytes(content)
        db.add(
            CampaignFile(campaign_id=campaign.id, file=f"mass_mail/{campaign.id}/{safe_name}")
        )
        saved += 1
    return saved, warnings


# --- Список ------------------------------------------------------------------


@router.get("/")
async def list_campaigns(
    request: Request, user: Profile = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    page_no = parse_page(request.query_params.get("page"))
    total = (
        await db.execute(
            select(func.count()).select_from(Campaign).where(Campaign.user_id == user.id)
        )
    ).scalar() or 0
    rows = await db.execute(
        select(Campaign)
        .where(Campaign.user_id == user.id)
        .order_by(Campaign.created_at.desc())
        .offset(offset(page_no, _PER_PAGE))
        .limit(_PER_PAGE)
    )
    page = Page(items=list(rows.scalars()), page=page_no, per_page=_PER_PAGE, total=total)
    return render(request, "campaigns/list.html", {"page": page})


# --- Создание ----------------------------------------------------------------


@router.get("/new")
async def new_campaign(
    request: Request, user: Profile = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    profiles = await _user_profiles(db, user)
    return render(
        request,
        "campaigns/form.html",
        {
            "campaign": None,
            "profiles": profiles,
            "templates": await _templates(db),
            "action": "/campaigns/",
        },
    )


@router.post("/", dependencies=[Depends(verify_csrf)])
async def create_campaign(
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    name: str = Form(""),
    subject: str = Form(""),
    message: str = Form(""),
    template_id: str = Form(""),
    recipient_emails: str = Form(""),
    stop_list: str = Form(""),
    use_smtp_settings_id: str = Form(""),
    message_count: str = Form("50"),
    message_interval: str = Form("2"),
    from_email: str = Form(""),
    from_name: str = Form(""),
    scheduled_time: str = Form(""),
    intent: str = Form("draft"),
    attachments: list[UploadFile] = File(default=[]),
):
    message = await _resolve_message(db, message, template_id)
    valid, invalid = split_emails(recipient_emails)
    error = _validate(name, subject, message, valid)
    scheduled = _parse_schedule(scheduled_time)
    # send_now не требует даты; schedule — требует
    if intent == "schedule" and scheduled is None:
        error = error or "Для планирования укажите корректные дату и время."

    if error:
        profiles = await _user_profiles(db, user)
        draft = _echo_form(
            name, subject, message, recipient_emails, stop_list,
            use_smtp_settings_id, message_count, message_interval,
            from_email, from_name, scheduled_time,
        )
        return render(
            request,
            "campaigns/form.html",
            {
                "campaign": draft,
                "profiles": profiles,
                "templates": await _templates(db),
                "action": "/campaigns/",
                "error": error,
            },
            status_code=400,
        )

    status = CampaignStatus.SCHEDULED.value if intent == "schedule" else CampaignStatus.DRAFT.value
    campaign = Campaign(
        name=name.strip(),
        subject=subject.strip(),
        message=message,
        recipient_emails=join_emails(valid),
        stop_list=join_emails(parse_emails(stop_list)),
        use_smtp_settings_id=_int_or_none(use_smtp_settings_id),
        message_count=clamp_batch_size(message_count),
        message_interval=clamp_interval(message_interval),
        from_email=from_email.strip() or None,
        from_name=from_name.strip() or None,
        scheduled_time=scheduled,
        status=status,
        total_recipients=len(valid),
        user_id=user.id,
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)

    _, warnings = await _save_attachments(db, campaign, attachments, existing=0)
    await db.commit()

    note = "Кампания создана."
    if invalid:
        warnings.append(f"Не приняты как адреса: {len(invalid)} шт.")
    if warnings:
        note += " " + " ".join(warnings)

    if intent == "send_now":
        from cbmail.workers.tasks import send_campaign_task
        send_campaign_task.delay(campaign.id, force=True)
        note = "Кампания создана, отправка запущена — обновите страницу через минуту."
    return redirect(f"/campaigns/{campaign.id}", ok=note)


# --- Детали ------------------------------------------------------------------


@router.get("/{campaign_id}")
async def campaign_detail(
    campaign_id: int,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await _get_owned(db, campaign_id, user)
    if campaign is None:
        return redirect("/campaigns/", err="Кампания не найдена.")

    logs = await db.execute(
        select(CampaignLog)
        .where(CampaignLog.campaign_id == campaign_id)
        .order_by(CampaignLog.created_at.desc())
        .limit(200)
    )
    counts = await db.execute(
        select(CampaignLog.status, func.count())
        .where(CampaignLog.campaign_id == campaign_id)
        .group_by(CampaignLog.status)
    )
    all_recipients = parse_emails(campaign.recipient_emails)
    # Предпросмотр «как в письме»: то же тело, что уйдёт адресату (с автоподписью
    # отписки), с подстановкой примера адреса вместо плейсхолдеров.
    profile = campaign.smtp
    preview = composer.compose(
        subject=campaign.subject,
        body=campaign.message,
        header=profile.message_header if profile else None,
        footer=profile.message_footer if profile else None,
        add_unsubscribe=True,
    ).personalize("example@mail.ru", "#")
    return render(
        request,
        "campaigns/detail.html",
        {
            "campaign": campaign,
            "logs": list(logs.scalars()),
            "log_counts": dict(counts.all()),
            "recipients_preview": all_recipients[:200],
            "recipients_total": len(all_recipients),
            "preview_html": preview.html_body,
            "preview_text": preview.text_body,
            "stoppable": campaign.status in _STOPPABLE,
        },
    )


# --- Редактирование ----------------------------------------------------------


@router.get("/{campaign_id}/edit")
async def edit_campaign(
    campaign_id: int,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await _get_owned(db, campaign_id, user)
    if campaign is None:
        return redirect("/campaigns/", err="Кампания не найдена.")
    if campaign.status == CampaignStatus.SENDING.value:
        return redirect(f"/campaigns/{campaign_id}", err="Нельзя редактировать идущую рассылку.")
    profiles = await _user_profiles(db, user)
    return render(
        request,
        "campaigns/form.html",
        {
            "campaign": campaign,
            "profiles": profiles,
            "templates": await _templates(db),
            "action": f"/campaigns/{campaign_id}/edit",
        },
    )


@router.post("/{campaign_id}/edit", dependencies=[Depends(verify_csrf)])
async def update_campaign(
    campaign_id: int,
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    name: str = Form(""),
    subject: str = Form(""),
    message: str = Form(""),
    template_id: str = Form(""),
    recipient_emails: str = Form(""),
    stop_list: str = Form(""),
    use_smtp_settings_id: str = Form(""),
    message_count: str = Form("50"),
    message_interval: str = Form("2"),
    from_email: str = Form(""),
    from_name: str = Form(""),
    scheduled_time: str = Form(""),
    intent: str = Form("draft"),
    attachments: list[UploadFile] = File(default=[]),
):
    campaign = await _get_owned(db, campaign_id, user)
    if campaign is None:
        return redirect("/campaigns/", err="Кампания не найдена.")
    if campaign.status == CampaignStatus.SENDING.value:
        return redirect(f"/campaigns/{campaign_id}", err="Нельзя редактировать идущую рассылку.")

    message = await _resolve_message(db, message, template_id)
    valid, invalid = split_emails(recipient_emails)
    error = _validate(name, subject, message, valid)
    scheduled = _parse_schedule(scheduled_time)
    if intent == "schedule" and scheduled is None:
        error = error or "Для планирования укажите корректные дату и время."
    if error:
        profiles = await _user_profiles(db, user)
        return render(
            request,
            "campaigns/form.html",
            {
                "campaign": campaign,
                "profiles": profiles,
                "templates": await _templates(db),
                "action": f"/campaigns/{campaign_id}/edit",
                "error": error,
            },
            status_code=400,
        )

    campaign.name = name.strip()
    campaign.subject = subject.strip()
    campaign.message = message
    campaign.recipient_emails = join_emails(valid)
    campaign.stop_list = join_emails(parse_emails(stop_list))
    campaign.use_smtp_settings_id = _int_or_none(use_smtp_settings_id)
    campaign.message_count = clamp_batch_size(message_count)
    campaign.message_interval = clamp_interval(message_interval)
    campaign.from_email = from_email.strip() or None
    campaign.from_name = from_name.strip() or None
    campaign.scheduled_time = scheduled
    campaign.total_recipients = len(valid)
    if intent == "schedule":
        campaign.status = CampaignStatus.SCHEDULED.value
    elif campaign.status == CampaignStatus.SCHEDULED.value and scheduled is None:
        campaign.status = CampaignStatus.DRAFT.value

    existing = await db.execute(
        select(func.count()).select_from(CampaignFile).where(CampaignFile.campaign_id == campaign_id)
    )
    _, warnings = await _save_attachments(db, campaign, attachments, existing=existing.scalar() or 0)
    await db.commit()

    note = "Изменения сохранены."
    if invalid:
        warnings.append(f"Не приняты как адреса: {len(invalid)} шт.")
    if warnings:
        note += " " + " ".join(warnings)
    return redirect(f"/campaigns/{campaign_id}", ok=note)


# --- СТОП / удаление / отправка ----------------------------------------------


@router.post("/{campaign_id}/stop", dependencies=[Depends(verify_csrf)])
async def stop_campaign(
    campaign_id: int,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await _get_owned(db, campaign_id, user)
    if campaign is None:
        return redirect("/campaigns/", err="Кампания не найдена.")
    if campaign.status not in _STOPPABLE:
        return redirect(f"/campaigns/{campaign_id}", err="Эту кампанию сейчас нельзя остановить.")
    campaign.status = CampaignStatus.CANCELLED.value
    db.add(
        CampaignLog(
            campaign_id=campaign_id,
            email="system",
            status="stopped",
            error_message="Остановлено пользователем",
        )
    )
    await db.commit()
    return redirect(f"/campaigns/{campaign_id}", ok="Рассылка остановлена. Кампания сохранена.")


@router.post("/{campaign_id}/delete", dependencies=[Depends(verify_csrf)])
async def delete_campaign(
    campaign_id: int,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await _get_owned(db, campaign_id, user)
    if campaign is None:
        return redirect("/campaigns/", err="Кампания не найдена.")
    await db.execute(delete(Campaign).where(Campaign.id == campaign_id))
    await db.commit()
    return redirect("/campaigns/", ok="Кампания удалена.")


@router.post("/{campaign_id}/send", dependencies=[Depends(verify_csrf)])
async def send_now(
    campaign_id: int,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await _get_owned(db, campaign_id, user)
    if campaign is None:
        return redirect("/campaigns/", err="Кампания не найдена.")
    if campaign.status in (CampaignStatus.SENDING.value, CampaignStatus.SENT.value):
        return redirect(f"/campaigns/{campaign_id}", err="Кампания уже отправляется или отправлена.")

    from cbmail.workers.tasks import send_campaign_task

    send_campaign_task.delay(campaign_id, force=True)
    return redirect(f"/campaigns/{campaign_id}", ok="Отправка запущена — обновите страницу через минуту.")


# --- Вспомогательное ---------------------------------------------------------


def _validate(name: str, subject: str, message: str, recipients: list[str]) -> str | None:
    if not name.strip():
        return "Укажите название кампании."
    if not subject.strip():
        return "Укажите тему письма."
    if not message.strip():
        return "Письмо не может быть пустым."
    if not recipients:
        return "Не указан ни один корректный адрес получателя."
    return None


def _int_or_none(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _echo_form(*args) -> dict:
    keys = [
        "name", "subject", "message", "recipient_emails", "stop_list",
        "use_smtp_settings_id", "message_count", "message_interval",
        "from_email", "from_name", "scheduled_time",
    ]
    data = dict(zip(keys, args, strict=False))
    data["id"] = None
    data["status"] = CampaignStatus.DRAFT.value
    return data
