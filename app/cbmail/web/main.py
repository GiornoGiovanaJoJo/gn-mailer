"""FastAPI-приложение: сборка, middleware, монтирование статики, роутеры.

Middleware выполняет три сквозные задачи одним проходом:

* **Лицензия.** Пока ключ недействителен — 503 на всё, кроме ``/license`` и
  ``/health``, чтобы клиент мог ввести новый ключ на заблокированном сервисе.
* **CSRF-сид.** Гарантирует cookie ``cbmail_csrf`` с непредсказуемым сидом, к
  которому привязываются токены форм.
* **Flash.** Прокидывает одноразовое сообщение из cookie в контекст шаблона.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from cbmail.core.config import get_settings
from cbmail.core.licensing import LICENSE_ALLOW_PREFIXES, verify_license
from cbmail.web.deps import FLASH_COOKIE, _RedirectException, read_flash
from cbmail.web.routers import (
    account,
    campaigns,
    license,
    messages,
    misc,
    public,
    smtp,
    stoplist,
    users,
)
from cbmail.web.routers import auth as auth_router
from cbmail.web.routers import templates as templates_router

_CSRF_COOKIE = "cbmail_csrf"
_CSRF_MAX_AGE = 60 * 60 * 24 * 14
# /unsubscribe должен работать всегда (комплаенс): даже при недействительной
# лицензии отписка не должна отдавать 503 — это ломало бы обязательную ссылку
# отписки в уже разосланных письмах.
_LICENSE_ALLOW = (*LICENSE_ALLOW_PREFIXES, "/health", "/unsubscribe")


def _license_block(reason: str) -> HTMLResponse:
    expired = "expired" in reason or "истёк" in reason
    title = "Срок лицензии истёк" if expired else "Сервис недоступен"
    note = (
        "Период использования завершён."
        if expired
        else "Лицензия недействительна для этого сервера."
    )
    return HTMLResponse(
        f"<!doctype html><meta charset='utf-8'><title>503</title>"
        f"<div style='font-family:sans-serif;max-width:640px;margin:15vh auto;text-align:center'>"
        f"<h1 style='font-size:2rem'>503 — {title}</h1>"
        f"<p style='color:#555'>{note}</p>"
        f"<p><a href='/license/' style='display:inline-block;margin-top:12px;"
        f"padding:10px 22px;background:#7367f0;color:#fff;border-radius:8px;"
        f"text-decoration:none'>Ввести лицензионный ключ</a></p></div>",
        status_code=503,
    )


def _response_sets_flash(response) -> bool:
    return any(
        name.lower() == b"set-cookie" and b"cbmail_flash=" in value
        for name, value in response.raw_headers
    )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="cbmail", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.secret_key = settings.secret_key

    @app.middleware("http")
    async def core_middleware(request, call_next):
        request.state.user = None
        request.state.flash = read_flash(request)

        seed = request.cookies.get(_CSRF_COOKIE)
        new_seed = None
        if not seed:
            seed = secrets.token_urlsafe(24)
            new_seed = seed
        request.state.csrf_seed = seed

        path = request.url.path
        ok, reason = verify_license()
        if not ok and not path.startswith(_LICENSE_ALLOW):
            response = _license_block(reason)
        else:
            response = await call_next(request)

        if new_seed:
            response.set_cookie(
                _CSRF_COOKIE, new_seed, httponly=True, samesite="lax", max_age=_CSRF_MAX_AGE
            )
        if request.state.flash is not None and not _response_sets_flash(response):
            response.delete_cookie(FLASH_COOKIE)
        return response

    @app.exception_handler(_RedirectException)
    async def _handle_redirect(request, exc: _RedirectException):
        return RedirectResponse(exc.url, status_code=303)

    _mount_static(app, settings)

    app.include_router(misc.router)
    app.include_router(license.router)
    app.include_router(auth_router.router)
    app.include_router(campaigns.router)
    app.include_router(messages.router)
    app.include_router(smtp.router)
    app.include_router(stoplist.router)
    app.include_router(templates_router.router)
    app.include_router(templates_router.image_router)
    app.include_router(users.router)
    app.include_router(account.router)
    app.include_router(public.router)
    return app


def _mount_static(app: FastAPI, settings) -> None:
    static_root = Path(settings.static_root)
    media_root = Path(settings.media_root)
    if static_root.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_root)), name="static")
    if media_root.is_dir():
        app.mount("/media", StaticFiles(directory=str(media_root)), name="media")


app = create_app()
