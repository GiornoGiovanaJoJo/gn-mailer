"""Конфигурация приложения.

Все настройки читаются из окружения один раз при старте и дальше неизменны.
Отсутствие обязательной переменной — ошибка запуска, а не сюрприз в рантайме:
в старой версии половина настроек бралась через ``getattr(settings, ..., default)``,
из-за чего опечатка в имени переменной тихо превращалась в дефолт.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Общие ---
    debug: bool = Field(default=False, alias="DJANGO_DEBUG")
    secret_key: str = Field(alias="DJANGO_SECRET_KEY")
    timezone: str = Field(default="Europe/Moscow", alias="TIME_ZONE")

    # --- База данных ---
    postgres_db: str = Field(alias="POSTGRES_DB")
    postgres_user: str = Field(alias="POSTGRES_USER")
    postgres_password: str = Field(alias="POSTGRES_PASSWORD")
    postgres_host: str = Field(default="localhost", alias="DJANGO_POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="DJANGO_POSTGRES_PORT")

    # --- Redis / Celery ---
    redis_host: str = Field(default="localhost", alias="DJANGO_REDIS_HOST")
    redis_port: int = Field(default=6379, alias="DJANGO_REDIS_PORT")

    # --- Файлы ---
    media_root: Path = Field(default=Path("_media"), alias="MEDIA_ROOT")
    static_root: Path = Field(default=Path("_static"), alias="STATIC_ROOT")
    media_url: str = "/media/"
    static_url: str = "/static/"

    # --- Лицензия ---
    license_file: Path | None = Field(default=None, alias="LICENSE_FILE")

    # --- Сессии ---
    session_cookie_name: str = "sessionid"
    session_max_age_seconds: int = 60 * 60 * 24 * 14  # 2 недели

    # --- Ограничения SMTP ---
    smtp_timeout_seconds: int = 30
    smtp_max_attachments: int = 3
    smtp_attachment_max_bytes: int = 10 * 1024 * 1024

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("debug", mode="before")
    @classmethod
    def _parse_debug(cls, value: object) -> bool:
        """DJANGO_DEBUG в .env записан как ``1  # 0|1 - false|true``.

        Значение с висящим комментарием ломало bool-приведение, поэтому режем
        по первому пробелу и принимаем привычные текстовые варианты.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str):
            head = value.split("#", 1)[0].strip().lower()
            return head in {"1", "true", "yes", "on"}
        return False

    @property
    def database_url(self) -> str:
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.postgres_user,
                password=self.postgres_password,
                host=self.postgres_host,
                port=self.postgres_port,
                path=self.postgres_db,
            )
        )

    @property
    def sync_database_url(self) -> str:
        """Синхронный URL — нужен Alembic и Celery-воркеру."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+psycopg",
                username=self.postgres_user,
                password=self.postgres_password,
                host=self.postgres_host,
                port=self.postgres_port,
                path=self.postgres_db,
            )
        )

    @property
    def redis_url(self) -> str:
        return str(
            RedisDsn.build(
                scheme="redis",
                host=self.redis_host,
                port=self.redis_port,
                path="0",
            )
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
