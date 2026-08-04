"""Корневой редирект и health-check."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, RedirectResponse

router = APIRouter()


@router.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/campaigns/", status_code=307)


@router.get("/health")
async def health() -> PlainTextResponse:
    # Открыт даже без лицензии — по нему оркестратор отличает «жив» от «заблокирован».
    return PlainTextResponse("ok")
