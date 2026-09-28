"""Публичные адреса трекинга: пиксель открытия и переход по ссылке.

Открываются из почтового клиента получателя — без сессии, без CSRF. Ровно
поэтому здесь нет ни одного действия, которое меняло бы что-то, кроме двух
счётчиков, и оба защищены подписью.

Открытие и переход засчитываются один раз на получателя: почтовые клиенты
подгружают картинку при каждом показе письма, и без этого ограничения
статистика показывала бы не «сколько человек открыли», а «сколько раз
прокрутили список писем».
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from cbmail.core.config import get_settings
from cbmail.db.models import Campaign, CampaignLog
from cbmail.domain.tracking import PIXEL_GIF, parse_token, verify_click
from cbmail.web.deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/t", tags=["tracking"])

#: Заголовки пикселя: никакого кеша, иначе повторное открытие письма не дойдёт
#: до сервера, а картинка останется в кеше клиента навсегда.
_PIXEL_HEADERS = {
    "Content-Type": "image/gif",
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
}


async def _mark(db: AsyncSession, token: str, column: str) -> None:
    """Отметить событие для получателя и поднять счётчик кампании.

    Условие ``column IS NULL`` в UPDATE делает операцию идемпотентной: сколько
    бы раз ни пришёл запрос, строка обновится однажды, и счётчик кампании
    вырастет ровно на единицу.
    """
    parsed = parse_token(get_settings().secret_key, token)
    if parsed is None:
        return
    campaign_id, email = parsed

    log_column = getattr(CampaignLog, column)

    result = await db.execute(
        update(CampaignLog)
        .where(
            CampaignLog.campaign_id == campaign_id,
            CampaignLog.email == email,
            log_column.is_(None),
        )
        .values(**{column: datetime.now(timezone.utc)})
    )
    if result.rowcount:
        counter = Campaign.opened_count if column == "opened_at" else Campaign.clicked_count
        await db.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(**{counter.key: counter + 1})
        )
    await db.commit()


@router.get("/o/{token}")
async def track_open(token: str, db: AsyncSession = Depends(get_db)) -> Response:
    """Пиксель открытия. Всегда отдаёт картинку — даже если токен чужой."""
    clean = token[:-4] if token.lower().endswith(".gif") else token
    try:
        await _mark(db, clean, "opened_at")
    except Exception:
        logger.exception("не удалось отметить открытие")
    return Response(content=PIXEL_GIF, headers=_PIXEL_HEADERS)


@router.get("/c/{token}")
async def track_click(
    token: str,
    u: str = Query(default=""),
    s: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Переход по ссылке из письма: засчитать и отправить дальше."""
    target = u.strip()
    secret = get_settings().secret_key
    # Только http(s): иначе через наш домен можно было бы отправить человека на
    # javascript: или data:.
    if not target.lower().startswith(("http://", "https://")):
        return Response(content="Некорректная ссылка", status_code=400, media_type="text/plain")
    # И только на адрес, который действительно был в этом письме: без этой
    # проверки ссылка превращается в открытый редирект для фишинга.
    if not verify_click(secret, token, target, s):
        return Response(content="Некорректная ссылка", status_code=400, media_type="text/plain")

    try:
        await _mark(db, token, "clicked_at")
    except Exception:
        logger.exception("не удалось отметить переход")
    return RedirectResponse(target, status_code=302)
