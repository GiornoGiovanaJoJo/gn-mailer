"""Общие фикстуры.

Тесты работают на SQLite в памяти: они должны запускаться одной командой без
поднятого Postgres, иначе их перестают гонять локально — то есть именно тогда,
когда они полезнее всего. Схема создаётся из тех же моделей, что идут в прод,
поэтому расхождение между тестовой и боевой структурой невозможно.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

# Конфигурация читается при импорте модулей приложения — задаём до них.
os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key")
os.environ.setdefault("POSTGRES_DB", "test")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from cbmail.core.security import hash_password
from cbmail.db.models import Base, Campaign, Profile, SmtpProfile, StopListRow


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def user(db: Session) -> Profile:
    profile = Profile(
        id=uuid.uuid4(),
        # Малое число итераций: тестам не нужна стойкость, а 1 000 000 итераций
        # на каждую фикстуру превратили бы прогон в минуты.
        password=hash_password("secret", iterations=1000),
        username="tester",
        first_name="Иван",
        last_name="Тестов",
        email="tester@example.com",
        is_active=True,
        is_staff=False,
        is_superuser=False,
        type=1,
        gender=1,
        avatar="",
        online=False,
        blocked=False,
        deleted=False,
    )
    db.add(profile)
    db.commit()
    return profile


@pytest.fixture
def smtp_profile(db: Session, user: Profile) -> SmtpProfile:
    profile = SmtpProfile(
        title="Основной",
        email_host="smtp.example.com",
        email_port="587",
        email_host_user="sender@example.com",
        email_host_password="пароль",
        default_from_email="sender@example.com",
        email_use_tls=True,
        email_use_ssl=False,
        user_id=user.id,
        message_header="Здравствуйте!",
        message_footer="С уважением, команда",
        last_check_success=False,
        check_count=0,
        success_count=0,
        failure_count=0,
    )
    db.add(profile)
    db.commit()
    return profile


@pytest.fixture
def stoplist(db: Session) -> StopListRow:
    row = StopListRow(id=1, list="")
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def make_campaign(db: Session, user: Profile, smtp_profile: SmtpProfile):
    """Фабрика кампаний с разумными умолчаниями."""

    def _make(**overrides: object) -> Campaign:
        defaults: dict[str, object] = {
            "name": "Тестовая рассылка",
            "subject": "Тема письма",
            "message": "Текст письма",
            "recipient_emails": "a@example.com, b@example.com",
            "stop_list": "",
            "message_count": 50,
            "message_interval": 0,
            "use_smtp_settings_id": smtp_profile.id,
            "status": "scheduled",
            "from_email": "sender@example.com",
            "from_name": "Отправитель",
            "total_recipients": 0,
            "sent_count": 0,
            "failed_count": 0,
            "opened_count": 0,
            "clicked_count": 0,
            "user_id": user.id,
        }
        defaults.update(overrides)
        campaign = Campaign(**defaults)  # type: ignore[arg-type]
        db.add(campaign)
        db.commit()
        return campaign

    return _make
