"""Глобальный стоп-лист: адреса, которым письма не уходят никогда.

Django хранит его одной строкой через запятую в единственной записи
``mail_stoplist``. Схему не меняем, но разбор/нормализация идут через домен.
UI повторяет оригинал: счётчик, добавление одного адреса, массовое добавление и
список в три колонки с удалением каждого адреса.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.db.models import Profile, StopListRow, Unsubscribe
from cbmail.domain.emails import (
    is_valid_email,
    join_emails,
    normalize_email,
    parse_emails,
    split_emails,
)
from cbmail.web.deps import get_db, redirect, require_user, verify_csrf
from cbmail.web.templating import render

router = APIRouter(prefix="/stoplist")


async def _get_row(db: AsyncSession) -> StopListRow:
    row = (
        await db.execute(select(StopListRow).order_by(StopListRow.id).limit(1))
    ).scalar_one_or_none()
    if row is None:
        row = StopListRow(list="")
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


def _columns(emails: list[str], count: int = 3) -> list[list[str]]:
    """Разложить адреса по ``count`` колонкам последовательными кусками."""
    size = max(1, (len(emails) + count - 1) // count)
    cols = [emails[i : i + size] for i in range(0, len(emails), size)]
    while len(cols) < count:
        cols.append([])
    return cols


async def _unsubscribes_by_sender(db: AsyncSession) -> list[dict]:
    """Отписавшиеся, сгруппированные по отправителю (домену).

    Показываются только для сведения: их источник — публичная ссылка отписки в
    письмах, а не ручной ввод. Пустая область — глобальная отписка (старые письма).
    """
    rows = (
        await db.execute(
            select(Unsubscribe.email, Unsubscribe.scope).order_by(
                Unsubscribe.scope, Unsubscribe.email
            )
        )
    ).all()
    groups: dict[str, list[str]] = {}
    for email, scope in rows:
        groups.setdefault(scope or "", []).append(email)
    return [
        {"label": scope or "Все отправители (глобально)", "emails": emails}
        for scope, emails in sorted(groups.items())
    ]


@router.get("/")
async def view_stoplist(
    request: Request, user: Profile = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    row = await _get_row(db)
    emails = parse_emails(row.list)
    unsub_groups = await _unsubscribes_by_sender(db)
    unsub_total = sum(len(g["emails"]) for g in unsub_groups)
    return render(
        request,
        "stoplist.html",
        {
            "emails": emails,
            "total_count": len(emails),
            "columns": _columns(emails),
            "unsub_groups": unsub_groups,
            "unsub_total": unsub_total,
        },
    )


@router.post("/", dependencies=[Depends(verify_csrf)])
async def update_stoplist(
    request: Request,
    user: Profile = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    action: str = Form("add_bulk"),
    email: str = Form(""),
    bulk_emails: str = Form(""),
):
    row = await _get_row(db)
    current = parse_emails(row.list)
    existing = {normalize_email(e) for e in current}

    if action == "remove":
        target = normalize_email(email)
        current = [e for e in current if normalize_email(e) != target]
        row.list = join_emails(current)
        await db.commit()
        return redirect("/stoplist/", ok="Адрес удалён из стоп-листа.")

    if action == "add_single":
        candidate = (email or "").strip()
        if not is_valid_email(candidate):
            return redirect("/stoplist/", err="Некорректный email-адрес.")
        if normalize_email(candidate) in existing:
            return redirect("/stoplist/", err="Этот адрес уже в стоп-листе.")
        current.append(candidate)
        row.list = join_emails(current)
        await db.commit()
        return redirect("/stoplist/", ok="Адрес добавлен в стоп-лист.")

    # add_bulk (по умолчанию)
    valid, invalid = split_emails(bulk_emails)
    added = 0
    for candidate in valid:
        if normalize_email(candidate) not in existing:
            current.append(candidate)
            existing.add(normalize_email(candidate))
            added += 1
    row.list = join_emails(current)
    await db.commit()
    note = f"Добавлено адресов: {added}."
    if invalid:
        note += f" Пропущено некорректных: {len(invalid)}."
    return redirect("/stoplist/", ok=note)
