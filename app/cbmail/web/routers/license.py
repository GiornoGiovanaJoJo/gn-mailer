"""Публичная страница активации лицензии.

CSRF намеренно не проверяется: защита здесь — подпись Ed25519 (принимается
только ключ, подписанный разработчиком для ЭТОГО хоста), а страница обязана
работать, когда остальной сервис заблокирован.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from cbmail.core.licensing import install_license, license_status
from cbmail.web.templating import render

router = APIRouter()


@router.get("/license")
@router.get("/license/")
async def license_page(request: Request):
    return render(request, "license.html", {"status": license_status(), "error": None})


@router.post("/license")
@router.post("/license/")
async def license_submit(request: Request, license_key: str = Form("")):
    ok, reason = install_license(license_key)
    if ok:
        return RedirectResponse("/", status_code=303)
    return render(
        request,
        "license.html",
        {"status": license_status(), "error": reason},
        status_code=400,
    )
