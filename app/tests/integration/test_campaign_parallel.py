"""Рассылка с нескольких ящиков одновременно.

Главное свойство здесь не скорость, а корректность дележа: каждый адрес должен
получить ровно одно письмо. Сбой проявляется не отказом, а тем, что один
человек получает два письма, а другой ни одного, — и узнать об этом можно
только из жалобы.

База — файловая SQLite, а не в памяти: подключение в памяти у каждого потока
своё, то есть своя пустая база. Файл даёт потокам ту же самую базу, что и
проверяемой кампании.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from cbmail.core.security import hash_password
from cbmail.db.models import (
    Base,
    Campaign,
    CampaignLog,
    CampaignSmtp,
    Profile,
    SmtpProfile,
    StopListRow,
)
from cbmail.services import campaign_sender
from cbmail.services.campaign_sender import send_campaign

UTC = timezone.utc
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


class RecordingSmtp:
    """Заглушка SMTP, запоминающая, какой ящик кому отправил."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_mailbox: dict[str, list[str]] = {}
        self.sent: list[str] = []

    def __call__(self, credentials: object) -> RecordingSession:
        return RecordingSession(self, getattr(credentials, "username", "?"))

    def record(self, mailbox: str, to: str) -> None:
        with self.lock:
            self.by_mailbox.setdefault(mailbox, []).append(to)
            self.sent.append(to)


class RecordingSession:
    def __init__(self, parent: RecordingSmtp, mailbox: str) -> None:
        self.parent = parent
        self.mailbox = mailbox

    def __enter__(self) -> RecordingSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def send(self, message: object, to: str) -> None:
        self.parent.record(self.mailbox, to)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> RecordingSmtp:
    fake = RecordingSmtp()
    monkeypatch.setattr(campaign_sender, "SmtpSession", fake)
    return fake


@pytest.fixture
def file_db(tmp_path) -> Iterator[Session]:
    """База в файле: потокам нужна общая, а не по собственной копии на каждый."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _mailbox(db: Session, user: Profile, name: str, address: str, limit: int = 0) -> SmtpProfile:
    profile = SmtpProfile(
        title=name,
        email_host="smtp.example.com",
        email_port="587",
        email_host_user=address,
        email_host_password="пароль",
        default_from_email=address,
        email_use_tls=True,
        email_use_ssl=False,
        user_id=user.id,
        daily_limit=limit,
        last_check_success=False,
        check_count=0,
        success_count=0,
        failure_count=0,
    )
    db.add(profile)
    db.commit()
    return profile


@pytest.fixture
def world(file_db: Session):
    """Пользователь, стоп-лист и фабрика кампаний на файловой базе."""
    user = Profile(
        id=uuid.uuid4(),
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
    file_db.add(user)
    file_db.add(StopListRow(id=1, list=""))
    file_db.commit()

    def make_campaign(recipients: list[str], mailboxes: list[SmtpProfile], **over: object) -> Campaign:
        campaign = Campaign(
            name="Рассылка",
            subject="Тема",
            message="Текст",
            recipient_emails=", ".join(recipients),
            stop_list="",
            message_count=50,
            message_interval=0,
            use_smtp_settings_id=mailboxes[0].id,
            status="scheduled",
            from_email=mailboxes[0].default_from_email,
            from_name="Отправитель",
            total_recipients=0,
            sent_count=0,
            failed_count=0,
            opened_count=0,
            clicked_count=0,
            user_id=user.id,
            **over,  # type: ignore[arg-type]
        )
        file_db.add(campaign)
        file_db.commit()
        for mailbox in mailboxes:
            file_db.add(CampaignSmtp(campaign_id=campaign.id, smtp_id=mailbox.id))
        file_db.commit()
        file_db.refresh(campaign)
        return campaign

    return user, make_campaign


def test_каждый_адрес_уходит_ровно_один_раз(file_db: Session, world, smtp: RecordingSmtp) -> None:
    user, make_campaign = world
    boxes = [_mailbox(file_db, user, f"Ящик {i}", f"box{i}@example.com") for i in range(3)]
    recipients = [f"u{i}@example.ru" for i in range(60)]

    result = send_campaign(file_db, make_campaign(recipients, boxes).id, now=lambda: NOW)

    assert result.sent == len(recipients)
    assert sorted(smtp.sent) == sorted(recipients), "ни один адрес не потерян и не задвоен"
    assert len(set(smtp.sent)) == len(recipients), "ни один адрес не получил двух писем"

    logged = file_db.execute(
        select(CampaignLog.email).where(CampaignLog.status == "sent")
    ).scalars().all()
    assert sorted(logged) == sorted(recipients)


def test_работают_все_ящики_а_не_один(file_db: Session, world, smtp: RecordingSmtp) -> None:
    user, make_campaign = world
    boxes = [_mailbox(file_db, user, f"Ящик {i}", f"box{i}@example.com") for i in range(3)]
    recipients = [f"u{i}@example.ru" for i in range(60)]

    send_campaign(file_db, make_campaign(recipients, boxes).id, now=lambda: NOW)

    assert len(smtp.by_mailbox) == 3, f"задействованы ящики: {list(smtp.by_mailbox)}"
    for mailbox, sent in smtp.by_mailbox.items():
        assert sent, f"ящик {mailbox} не отправил ничего"


def test_в_логе_записан_ящик_которым_отправлено(file_db: Session, world, smtp: RecordingSmtp) -> None:
    """Без этого суточный лимит на ящик посчитать нечем: он принадлежит ящику."""
    user, make_campaign = world
    boxes = [_mailbox(file_db, user, f"Ящик {i}", f"box{i}@example.com") for i in range(2)]

    send_campaign(file_db, make_campaign([f"u{i}@example.ru" for i in range(20)], boxes).id, now=lambda: NOW)

    rows = file_db.execute(
        select(CampaignLog.smtp_id).where(CampaignLog.status == "sent")
    ).scalars().all()
    assert all(value is not None for value in rows), "ящик проставлен у каждой записи"
    assert set(rows) == {box.id for box in boxes}, "в логе отмечены оба ящика"


def test_суточный_лимит_считается_по_каждому_ящику(file_db: Session, world, smtp: RecordingSmtp) -> None:
    """Лимит провайдера — на ящик. Два ящика по 10 дают 20, а не 10 на двоих."""
    user, make_campaign = world
    boxes = [_mailbox(file_db, user, f"Ящик {i}", f"box{i}@example.com", limit=10) for i in range(2)]
    recipients = [f"u{i}@example.ru" for i in range(50)]

    result = send_campaign(file_db, make_campaign(recipients, boxes).id, now=lambda: NOW)

    assert result.sent == 20, "суммарная квота двух ящиков"
    assert result.deferred, "остаток перенесён на следующие сутки, а не потерян"
    for mailbox, sent in smtp.by_mailbox.items():
        assert len(sent) <= 10, f"ящик {mailbox} превысил свою квоту: {len(sent)}"


def test_без_выбранных_ящиков_работает_прежний_путь(file_db: Session, world, smtp: RecordingSmtp) -> None:
    """Созданные раньше рассылки не меняют поведения: один ящик, один поток."""
    user, make_campaign = world
    box = _mailbox(file_db, user, "Единственный", "solo@example.com")
    campaign = make_campaign([f"u{i}@example.ru" for i in range(5)], [box])
    # Убираем связь — остаётся только use_smtp_settings_id, как у старых кампаний.
    file_db.query(CampaignSmtp).delete()
    file_db.commit()
    file_db.refresh(campaign)

    result = send_campaign(file_db, campaign.id, now=lambda: NOW)

    assert result.sent == 5
    assert list(smtp.by_mailbox) == ["solo@example.com"]
