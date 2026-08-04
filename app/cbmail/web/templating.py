"""Настройка Jinja2 и общие для всех шаблонов значения.

Шаблоны новые и компактные: старая Django-панель — это огромный Sneat-темплейт
на десятки разделов (блоги, склад, HR…), которых у почтового продукта нет.
Переносить их целиком означало бы тащить мёртвый код; вместо этого — чистый
минимум страниц, покрывающих рассылки, профили SMTP и стоп-лист.
"""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.templating import Jinja2Templates

from cbmail.core.security import CSRF_FIELD_NAME, issue_csrf_token
from cbmail.domain.campaign import CampaignStatus

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_STATUS_BADGE = {
    CampaignStatus.DRAFT.value: "secondary",
    CampaignStatus.SCHEDULED.value: "info",
    CampaignStatus.SENDING.value: "warning",
    CampaignStatus.SENT.value: "success",
    CampaignStatus.CANCELLED.value: "danger",
}


def status_label(value: str) -> str:
    try:
        return CampaignStatus(value).label
    except ValueError:
        return value


def status_badge(value: str) -> str:
    return _STATUS_BADGE.get(value, "secondary")


def _csrf_seed(request: Request) -> str:
    return getattr(request.state, "csrf_seed", "") or ""


def _secret(request: Request) -> str:
    return request.app.state.secret_key


def csrf_token(request: Request) -> str:
    seed = _csrf_seed(request)
    if not seed:
        return ""
    return issue_csrf_token(seed, _secret(request))


def csrf_input(request: Request) -> str:
    token = csrf_token(request)
    return f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{token}">'


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["csrf_input"] = csrf_input
templates.env.globals["csrf_token"] = csrf_token
templates.env.filters["status_label"] = status_label
templates.env.filters["status_badge"] = status_badge


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {"user": getattr(request.state, "user", None)}
    if context:
        ctx.update(context)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
