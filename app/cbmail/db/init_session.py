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

from cbmail.db.models import Session, Unsubscribe
from cbmail.db.session import get_sync_engine

logger = logging.getLogger(__name__)

# Аддитивные таблицы приложения. Порядок не важен — зависимостей между ними нет.
_NEW_TABLES = (Session, Unsubscribe)


def ensure_app_tables() -> None:
    engine = get_sync_engine()
    for model in _NEW_TABLES:
        # checkfirst=True => CREATE выполняется, только если таблицы ещё нет.
        model.__table__.create(bind=engine, checkfirst=True)
        logger.info("Таблица %s готова", model.__tablename__)


# Прежнее имя — чтобы не ломать внешние вызовы, если где-то остались.
def ensure_session_table() -> None:
    ensure_app_tables()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_app_tables()
    print("cbmail tables: ok")
