"""Локальный запуск FastAPI-версии для БРАУЗЕРНОЙ сверки (не для прода).

Поднимает приложение на SQLite с демо-данными, отдаёт статику Sneat из
``src/_static`` и обходит лицензию — чтобы посмотреть реальный вид страниц в
браузере (скриншоты, тёмная тема, адаптив), не поднимая Postgres/Redis.

Запуск:  python tools/devserver.py   (слушает 127.0.0.1:8011)
Вход:    admin / dev
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "src" / "_static"
MEDIA = REPO / "src" / "_media"

os.environ.setdefault("DJANGO_SECRET_KEY", "dev-secret")
os.environ.setdefault("POSTGRES_DB", "dev")
os.environ.setdefault("POSTGRES_USER", "dev")
os.environ.setdefault("POSTGRES_PASSWORD", "dev")
os.environ["STATIC_ROOT"] = str(STATIC)
os.environ["MEDIA_ROOT"] = str(MEDIA)

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as OrmSession

from cbmail.core.security import hash_password
from cbmail.db.models import (
    Base,
    Campaign,
    Message,
    MessageTemplate,
    Profile,
    SmtpProfile,
    StopListRow,
)

_DB = REPO / "app" / "_devserver.sqlite3"


def _seed() -> None:
    if _DB.exists():
        _DB.unlink()
    engine = create_engine(f"sqlite:///{_DB}", future=True)
    Base.metadata.create_all(engine)
    uid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with OrmSession(engine) as s:
        s.add(
            Profile(
                id=uid, password=hash_password("dev", iterations=1000), username="admin",
                first_name="Администратор", last_name="", email="admin@example.com",
                is_active=True, is_staff=True, is_superuser=True, type=1, gender=1,
                avatar="", online=False, blocked=False, deleted=False,
            )
        )
        s.add(SmtpProfile(
            title="Основной (Яндекс)", email_host="smtp.yandex.ru", email_port="465",
            email_host_user="hello@example.ru", email_host_password="x",
            default_from_email="hello@example.ru", email_use_ssl=True, email_use_tls=False,
            user_id=uid, last_check_time=now, last_check_success=True, check_count=3,
            success_count=3, failure_count=0,
        ))
        s.add_all([
            Campaign(name="Летняя акция", subject="Скидки до 50% на весь ассортимент",
                     message=(
                         '<div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">'
                         '<h1 style="color:#6f5cff">Летняя распродажа началась!</h1>'
                         '<p>Здравствуйте! Только до конца недели — скидки до 50% на весь каталог.</p>'
                         '<p><a href="#" style="background:#6f5cff;color:#fff;padding:10px 20px;'
                         'border-radius:6px;text-decoration:none">Перейти в магазин</a></p>'
                         '<p style="color:#888">С уважением, команда магазина.</p></div>'
                     ),
                     recipient_emails="anna@example.com, ivan@example.com, petr@example.com, maria@example.com",
                     status="sent", total_recipients=1200, sent_count=1197, failed_count=3,
                     opened_count=418, user_id=uid, sent_at=now - timedelta(days=1)),
            Campaign(name="Анонс вебинара", subject="Приглашаем", message="<p>Ждём вас</p>",
                     recipient_emails="c@example.com", status="scheduled",
                     scheduled_time=now + timedelta(days=1), total_recipients=340, user_id=uid),
            Campaign(name="Черновик рассылки", subject="", message="", status="draft",
                     total_recipients=0, user_id=uid),
        ])
        s.add_all([
            MessageTemplate(name="Приветственное письмо", mask="<h1>Здравствуйте!</h1>", mask_json="{}"),
            MessageTemplate(name="Новостная рассылка", mask="<h2>Новости</h2>", mask_json="{}"),
        ])
        s.add_all([
            Message(id=uuid.uuid4(), custom_id="MSG-1001", user_id=uid, clients="client@example.com",
                    message="<p>Тело письма номер один</p>", subject="Договор на подпись",
                    message_type="outgoing", self_field=False),
            Message(id=uuid.uuid4(), custom_id="MSG-1002", user_id=uid, clients="lead@example.com",
                    message="Обычный текст без разметки.", subject="Коммерческое предложение",
                    message_type="incoming", self_field=False),
        ])
        s.add(StopListRow(list="unsub1@example.com, unsub2@example.com"))
        s.commit()
    engine.dispose()


def build_app():
    _seed()
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{_DB}", future=True)
    factory = async_sessionmaker(async_engine, expire_on_commit=False, autoflush=False)

    async def _get_db():
        async with factory() as session:
            yield session

    import cbmail.web.main as main_module

    main_module.verify_license = lambda *a, **k: (True, "ok")  # dev: лицензию не проверяем

    from cbmail.web.deps import get_db

    app = main_module.create_app()
    app.dependency_overrides[get_db] = _get_db
    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8011, log_level="warning")
