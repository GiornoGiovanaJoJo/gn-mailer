"""Подключения к БД.

Два движка по необходимости, а не по вкусу:

* веб-слой асинхронный — обработчик не должен блокировать event loop;
* Celery-воркер синхронный, и оборачивать его в ``asyncio.run`` ради ORM —
  лишний слой, который только усложняет диагностику зависаний.

Обе фабрики отдают сессии с ``expire_on_commit=False``: иначе после коммита
любое обращение к полю объекта выполняло бы новый SELECT, а в цикле отправки
это давало бы по два лишних запроса на каждое письмо.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from cbmail.core.config import get_settings


@lru_cache(maxsize=1)
def get_async_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=10,
        max_overflow=20,
        # Соединение, простоявшее дольше получаса, может быть уже закрыто
        # со стороны сервера; pre_ping отсеивает такие до запроса.
        pool_pre_ping=True,
        pool_recycle=1800,
    )


@lru_cache(maxsize=1)
def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_async_engine(), expire_on_commit=False, autoflush=False
    )


@lru_cache(maxsize=1)
def get_sync_engine():
    settings = get_settings()
    return create_engine(
        settings.sync_database_url,
        echo=False,
        # Воркер с concurrency=1 не нуждается в большом пуле, но соединение
        # обязано переживать долгие паузы между письмами.
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


@lru_cache(maxsize=1)
def get_sync_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_sync_engine(), expire_on_commit=False, autoflush=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI: сессия на время запроса.

    Коммит выполняется явно в сервисах. Здесь — только откат при исключении:
    незакрытая транзакция удерживала бы блокировки строк до таймаута.
    """
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def async_session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия вне запроса (стартовые задачи, скрипты)."""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@contextmanager
def sync_session_scope() -> Iterator[Session]:
    """Транзакция для Celery-задач.

    Коммит на успешном выходе, откат при исключении. В Django-версии явных
    транзакций не было вообще, поэтому падение посреди отправки оставляло
    кампанию с частично обновлёнными счётчиками и статусом «Отправляется».
    """
    factory = get_sync_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
