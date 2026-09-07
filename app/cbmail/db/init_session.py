"""Идемпотентное создание новых аддитивных таблиц приложения.

Вся остальная схема уже создана Django-миграциями и НЕ трогается: модели в
:mod:`cbmail.db.models` описывают существующие таблицы. Полноценный Alembic для
пары аддитивных таблиц избыточен и добавил бы риск разъехаться с боевой
схемой; вместо него — точечный ``CREATE TABLE IF NOT EXISTS`` через SQLAlchemy.

Новые таблицы (обе с префиксом ``cbmail_``, чужие Django-таблицы не задеты):

* ``cbmail_session`` — серверные сессии веб-приложения;
* ``cbmail_unsubscribe`` — отписки, привязанные к домену отправителя.

Запускается из entrypoint перед стартом веб-процесса.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from cbmail.db.models import MailProject, Session, Unsubscribe
from cbmail.db.session import get_sync_engine

logger = logging.getLogger(__name__)

# Аддитивные таблицы приложения. Порядок не важен — зависимостей между ними нет.
# ``mail_mailproject`` — новая таблица «проектов рассылки»; имя следует
# Django-конвенции, поэтому старая копия (Django-миграция) и новая (этот
# CREATE IF NOT EXISTS) сходятся к одной и той же схеме.
_NEW_TABLES = (Session, Unsubscribe, MailProject)

# Колонки, добавляемые к СУЩЕСТВУЮЩЕЙ таблице SMTP-профилей. create() их не
# трогает (таблица уже есть), поэтому добавляем точечным идемпотентным ALTER.
# ``ADD COLUMN IF NOT EXISTS`` — Postgres-специфично; на SQLite (тесты) эти
# колонки и так создаются из моделей через create_all, поэтому ALTER пропускаем.
_SMTP_COLUMN_ALTERS = (
    'ALTER TABLE mail_usersettingssmtp ADD COLUMN IF NOT EXISTS daily_limit integer NOT NULL DEFAULT 0',
    'ALTER TABLE mail_usersettingssmtp ADD COLUMN IF NOT EXISTS project_id bigint '
    'REFERENCES mail_mailproject(id) ON DELETE SET NULL',
)


def ensure_app_tables() -> None:
    engine = get_sync_engine()
    for model in _NEW_TABLES:
        # checkfirst=True => CREATE выполняется, только если таблицы ещё нет.
        model.__table__.create(bind=engine, checkfirst=True)
        logger.info("Таблица %s готова", model.__tablename__)

    # Колонки суточного лимита и проекта на профиле SMTP (только Postgres).
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            for ddl in _SMTP_COLUMN_ALTERS:
                conn.execute(text(ddl))
        logger.info("Колонки daily_limit/project_id на mail_usersettingssmtp готовы")


# Прежнее имя — чтобы не ломать внешние вызовы, если где-то остались.
def ensure_session_table() -> None:
    ensure_app_tables()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_app_tables()
    print("cbmail tables: ok")
