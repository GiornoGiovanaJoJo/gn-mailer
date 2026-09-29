"""Что видит человек, когда что-то сломалось.

Раньше при любой неожиданной ошибке приходила стандартная страница
«Internal Server Error» — ни намёка на причину, ни зацепки для разбора.
Пользователь сообщал «пишет внутреннюю ошибку», и сопоставить это с записью
в логе было нечем: за день там тысячи строк.

Теперь у каждой такой ошибки есть короткий код. Он печатается и на странице,
и в логе рядом с трассировкой — по нему нужная запись находится поиском за
секунду, даже если обращение пришло через неделю.

Отдельно распознаётся случай «схема базы отстала от кода»: не хватает колонки
или таблицы. Так выглядит установка, где обновили образ, но не выполнили
подготовку базы. Причина называется прямо, вместе с командой, которой чинится, —
иначе её ищут в своих данных, а она в развёртывании.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

logger = logging.getLogger(__name__)

#: Признаки того, что база не догнала код. Драйверы формулируют по-разному,
#: поэтому смотрим на текст: SQLSTATE 42703 (нет колонки) и 42P01 (нет таблицы).
_SCHEMA_MARKERS = (
    "undefinedcolumn",
    "undefinedtable",
    "does not exist",
    "не существует",
    "42703",
    "42p01",
)

_SCHEMA_HINT = (
    "База данных отстала от версии приложения: в ней нет колонки или таблицы, "
    "которую ждёт код. Выполните подготовку базы и перезапустите сервис: "
    "docker compose exec cbmail-web python -m cbmail.db.init_session"
)


def _looks_like_schema_drift(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _SCHEMA_MARKERS)


def _wants_html(request: Request) -> bool:
    """Браузеру — страницу, программе — JSON."""
    return "text/html" in request.headers.get("accept", "")


def _page(title: str, body: str, code: str) -> str:
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>{title}</title>
<style>
 body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
       margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center;
       background:#f7f7fb;color:#1f2937}}
 .card{{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:32px 36px;
        max-width:560px;box-shadow:0 8px 30px rgba(0,0,0,.06)}}
 h1{{margin:0 0 12px;font-size:20px}}
 p{{margin:8px 0;line-height:1.55}}
 code{{background:#f0f0f5;padding:2px 6px;border-radius:5px;font-size:13px;
       word-break:break-all}}
 .muted{{color:#6b7280;font-size:14px}}
 a{{color:#6f5cff}}
</style></head>
<body><div class="card">
<h1>{title}</h1>
{body}
<p class="muted">Код обращения: <code>{code}</code> — назовите его при обращении в поддержку.</p>
<p><a href="/">Вернуться на главную</a></p>
</div></body></html>"""


def install_error_handlers(app: FastAPI) -> None:
    """Повесить обработчик неожиданных ошибок на приложение."""

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        code = secrets.token_hex(4)
        logger.exception("[error %s] %s %s", code, request.method, request.url.path)

        if _looks_like_schema_drift(exc):
            if _wants_html(request):
                return HTMLResponse(
                    _page(
                        "База данных не готова",
                        f"<p>{_SCHEMA_HINT}</p>",
                        code,
                    ),
                    status_code=503,
                )
            return JSONResponse(
                {"error": _SCHEMA_HINT, "code": "SCHEMA_OUT_OF_DATE", "incident": code},
                status_code=503,
            )

        if _wants_html(request):
            return HTMLResponse(
                _page(
                    "Что-то пошло не так",
                    "<p>Действие не выполнено из-за ошибки на сервере. "
                    "Повторите попытку — если повторится, сообщите код ниже.</p>",
                    code,
                ),
                status_code=500,
            )
        return JSONResponse(
            {"error": "Внутренняя ошибка сервера", "incident": code}, status_code=500
        )
