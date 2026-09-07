"""Тесты отправки кампании — самая ответственная часть системы.

SMTP подменяется заглушкой: проверяется логика отправки, а не работа сети.
Каждый тест с меткой ``regression`` воспроизводит дефект Django-версии,
приводивший к неотправленной или, что хуже, дважды отправленной рассылке.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cbmail.db.models import Campaign, CampaignLog
from cbmail.services import campaign_sender
from cbmail.services.campaign_sender import (
    CampaignNotSendable,
    find_due_campaigns,
    recover_stuck_campaigns,
    send_campaign,
)
from cbmail.smtp.sender import SmtpConnectionError, SmtpRecipientError

UTC = timezone.utc
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


class FakeSmtp:
    """Заглушка SMTP-сессии: запоминает адреса и умеет падать по сценарию."""

    def __init__(self, fail_for: dict[str, Exception] | None = None) -> None:
        self.sent: list[str] = []
        self.fail_for = fail_for or {}
        self.connections = 0

    def __call__(self, credentials: object) -> FakeSmtp:
        self.connections += 1
        return self

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def send(self, message: object, to: str) -> None:
        if to in self.fail_for:
            raise self.fail_for[to]
        self.sent.append(to)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> FakeSmtp:
    fake = FakeSmtp()
    monkeypatch.setattr(campaign_sender, "SmtpSession", fake)
    return fake


def _now() -> datetime:
    return NOW


def _no_sleep(_: float) -> None:
    return None


def _send(db: Session, campaign_id: int, **kwargs: object):
    return send_campaign(
        db, campaign_id, sleep=_no_sleep, now=_now, **kwargs  # type: ignore[arg-type]
    )


class TestHappyPath:
    def test_sends_to_all_recipients(self, db: Session, make_campaign, smtp: FakeSmtp) -> None:
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru, c@x.ru")

        result = _send(db, campaign.id)

        assert smtp.sent == ["a@x.ru", "b@x.ru", "c@x.ru"]
        assert result.sent == 3
        assert result.failed == 0

    def test_marks_campaign_sent(self, db: Session, make_campaign, smtp: FakeSmtp) -> None:
        campaign = make_campaign()
        _send(db, campaign.id)

        db.expire_all()
        refreshed = db.get(Campaign, campaign.id)
        assert refreshed is not None
        assert refreshed.status == "sent"
        assert refreshed.sent_at is not None

    def test_writes_log_per_recipient(self, db: Session, make_campaign, smtp: FakeSmtp) -> None:
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru")
        _send(db, campaign.id)

        logs = db.execute(
            select(CampaignLog).where(CampaignLog.campaign_id == campaign.id)
        ).scalars().all()
        assert {log.email for log in logs} == {"a@x.ru", "b@x.ru"}
        assert all(log.status == "sent" for log in logs)

    @pytest.mark.regression
    def test_uses_single_connection_per_batch(
        self, db: Session, make_campaign, smtp: FakeSmtp
    ) -> None:
        """Раньше на каждое письмо приходилось по два подключения с авторизацией.

        Провайдеры блокировали такую активность как подозрительную, а скорость
        рассылки падала вдвое.
        """
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru, c@x.ru", message_count=50)
        _send(db, campaign.id)

        assert smtp.connections == 1

    def test_counters_are_updated(self, db: Session, make_campaign, smtp: FakeSmtp) -> None:
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru")
        _send(db, campaign.id)

        db.expire_all()
        refreshed = db.get(Campaign, campaign.id)
        assert refreshed is not None
        assert refreshed.sent_count == 2
        assert refreshed.total_recipients == 2


class TestDoubleSendProtection:
    @pytest.mark.regression
    def test_second_concurrent_run_is_rejected(
        self, db: Session, make_campaign, smtp: FakeSmtp
    ) -> None:
        """Кампания в статусе «Отправляется» не может быть запущена второй раз.

        Проверка статуса и его установка выполнялись раздельно, поэтому двойной
        клик или повторная доставка задачи Celery запускали ДВЕ параллельные
        отправки с первого получателя — пользователи получали письма дважды.
        """
        campaign = make_campaign(status="sending")

        with pytest.raises(CampaignNotSendable, match="уже выполняется"):
            _send(db, campaign.id, force=True)

        assert smtp.sent == []

    @pytest.mark.regression
    def test_force_does_not_resend_already_sent_campaign(
        self, db: Session, make_campaign, smtp: FakeSmtp
    ) -> None:
        """``force`` не должен обходить защиту от повторной отправки.

        Кнопка «Отправить сейчас» ставит задачу в очередь. Если за время
        ожидания расписание отправило кампанию само, форс-запуск рассылал всё
        повторно по всей базе.
        """
        campaign = make_campaign(status="sent")

        with pytest.raises(CampaignNotSendable, match="уже отправлена"):
            _send(db, campaign.id, force=True)

        assert smtp.sent == []

    @pytest.mark.regression
    def test_resume_skips_already_delivered(
        self, db: Session, make_campaign, smtp: FakeSmtp
    ) -> None:
        """Возобновление отменённой кампании не шлёт повторно тем, кто получил."""
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru, c@x.ru", status="cancelled")
        db.add(CampaignLog(campaign_id=campaign.id, email="a@x.ru", status="sent"))
        db.commit()

        result = _send(db, campaign.id, force=True)

        assert smtp.sent == ["b@x.ru", "c@x.ru"]
        assert result.sent == 2

    @pytest.mark.regression
    def test_resume_matches_already_sent_case_insensitively(
        self, db: Session, make_campaign, smtp: FakeSmtp
    ) -> None:
        """Лог сравнивался точным совпадением, а стоп-лист — нет.

        Адрес, записанный в логе в другом регистре, получал письмо повторно.
        """
        campaign = make_campaign(recipient_emails="Ivan@Mail.ru, b@x.ru", status="cancelled")
        db.add(CampaignLog(campaign_id=campaign.id, email="ivan@mail.ru", status="sent"))
        db.commit()

        _send(db, campaign.id, force=True)

        assert smtp.sent == ["b@x.ru"]

    def test_fully_delivered_campaign_completes_without_error(
        self, db: Session, make_campaign, smtp: FakeSmtp
    ) -> None:
        campaign = make_campaign(recipient_emails="a@x.ru", status="cancelled")
        db.add(CampaignLog(campaign_id=campaign.id, email="a@x.ru", status="sent"))
        db.commit()

        result = _send(db, campaign.id, force=True)

        assert result.already_complete
        db.expire_all()
        assert db.get(Campaign, campaign.id).status == "sent"  # type: ignore[union-attr]


class TestStopList:
    def test_global_stoplist_blocks_recipient(
        self, db: Session, make_campaign, smtp: FakeSmtp, stoplist
    ) -> None:
        stoplist.list = "b@x.ru"
        db.commit()
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru")

        _send(db, campaign.id)

        assert smtp.sent == ["a@x.ru"]

    @pytest.mark.regression
    def test_stoplist_is_case_insensitive(
        self, db: Session, make_campaign, smtp: FakeSmtp, stoplist
    ) -> None:
        stoplist.list = "IVAN@MAIL.RU"
        db.commit()
        campaign = make_campaign(recipient_emails="ivan@mail.ru, b@x.ru")

        _send(db, campaign.id)

        assert smtp.sent == ["b@x.ru"]

    def test_campaign_stoplist_blocks_recipient(
        self, db: Session, make_campaign, smtp: FakeSmtp
    ) -> None:
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru", stop_list="a@x.ru")

        _send(db, campaign.id)

        assert smtp.sent == ["b@x.ru"]

    def test_unsubscribe_is_scoped_to_sender_domain(
        self, db: Session, make_campaign, smtp: FakeSmtp
    ) -> None:
        """Отписка от одного отправителя не блокирует письма другого.

        Отправитель кампании — ``sender@example.com`` (домен ``example.com``).
        Адрес, отписавшийся от домена ``other.ru``, должен получить письмо;
        отписавшийся от ``example.com`` — нет.
        """
        from cbmail.db.models import Unsubscribe

        db.add(Unsubscribe(email="a@x.ru", scope="other.ru"))     # другой отправитель
        db.add(Unsubscribe(email="b@x.ru", scope="example.com"))  # этот отправитель
        db.commit()

        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru")
        _send(db, campaign.id)

        assert smtp.sent == ["a@x.ru"]

    def test_global_unsubscribe_blocks_every_sender(
        self, db: Session, make_campaign, smtp: FakeSmtp
    ) -> None:
        """Отписка без области (старые письма) действует на всех отправителей."""
        from cbmail.db.models import Unsubscribe

        db.add(Unsubscribe(email="b@x.ru", scope=""))
        db.commit()

        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru")
        _send(db, campaign.id)

        assert smtp.sent == ["a@x.ru"]

    @pytest.mark.regression
    def test_duplicate_recipients_get_one_email(
        self, db: Session, make_campaign, smtp: FakeSmtp
    ) -> None:
        """Список получателей не дедуплицировался — дубли получали два письма."""
        campaign = make_campaign(recipient_emails="a@x.ru, a@x.ru, A@X.RU")

        _send(db, campaign.id)

        assert smtp.sent == ["a@x.ru"]


class TestFailures:
    def test_permanent_failure_is_logged_and_send_continues(
        self, db: Session, make_campaign, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeSmtp(
            fail_for={"bad@x.ru": SmtpRecipientError("Адрес не существует", permanent=True)}
        )
        monkeypatch.setattr(campaign_sender, "SmtpSession", fake)
        campaign = make_campaign(recipient_emails="a@x.ru, bad@x.ru, c@x.ru")

        result = _send(db, campaign.id)

        assert fake.sent == ["a@x.ru", "c@x.ru"]
        assert result.sent == 2
        assert result.failed == 1

    @pytest.mark.regression
    def test_connection_loss_reconnects_and_continues(
        self, db: Session, make_campaign, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Обрыв соединения на одном адресе не должен «съедать» идущих за ним.

        Провайдеры (в частности mail.ru) периодически рвут соединение на
        очередном письме. Раньше все адреса ПОСЛЕ оборвавшегося молча
        пропускались, а кампания помечалась «Отправлена» — эти люди не получали
        письмо никогда. Теперь соединение переоткрывается и отправка
        продолжается со следующего адреса.
        """
        fake = FakeSmtp(fail_for={"b@x.ru": SmtpConnectionError("Соединение разорвано")})
        monkeypatch.setattr(campaign_sender, "SmtpSession", fake)
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru, c@x.ru, d@x.ru")

        result = _send(db, campaign.id)

        failed = db.execute(
            select(CampaignLog.email).where(
                CampaignLog.campaign_id == campaign.id, CampaignLog.status == "failed"
            )
        ).scalars().all()
        # Неудачным помечен только тот адрес, на котором реально оборвалось.
        assert failed == ["b@x.ru"]
        # Идущие ПОСЛЕ обрыва адреса отправлены (соединение переоткрылось).
        sent = db.execute(
            select(CampaignLog.email).where(
                CampaignLog.campaign_id == campaign.id, CampaignLog.status == "sent"
            )
        ).scalars().all()
        assert set(sent) == {"a@x.ru", "c@x.ru", "d@x.ru"}
        assert result.sent == 3
        assert result.failed == 1
        # Все адреса обработаны за один заход — кампания завершена.
        db.expire_all()
        assert db.get(Campaign, campaign.id).status == "sent"  # type: ignore[union-attr]

    @pytest.mark.regression
    def test_repeated_drops_beyond_budget_requeue_remaining(
        self, db: Session, make_campaign, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Если соединение рвётся чаще бюджета переподключений — остаток на повтор.

        Кампания НЕ помечается «Отправлена», а ставится на повтор, чтобы
        необработанные адреса ушли позже, а не потерялись.
        """
        # Первые адреса рвут соединение при каждой отправке — бюджет исчерпывается.
        drops = {f"drop{i}@x.ru": SmtpConnectionError("обрыв") for i in range(7)}
        fake = FakeSmtp(fail_for=drops)
        monkeypatch.setattr(campaign_sender, "SmtpSession", fake)
        recipients = ", ".join([f"drop{i}@x.ru" for i in range(7)] + ["ok@x.ru"])
        campaign = make_campaign(recipient_emails=recipients)

        result = _send(db, campaign.id)

        assert result.interrupted
        db.expire_all()
        refreshed = db.get(Campaign, campaign.id)
        assert refreshed is not None
        # Есть прогресс не гарантирован (все drop* упали), но раз ни одного sent —
        # кампания возвращается в черновик, а не крутится в повторах.
        assert refreshed.status in {"scheduled", "draft"}

    @pytest.mark.regression
    def test_campaign_without_recipients_returns_to_draft_keeping_schedule(
        self, db: Session, make_campaign, smtp: FakeSmtp
    ) -> None:
        """Расписание не должно стираться при ошибке подготовки.

        Старый код обнулял ``scheduled_time``, и пользователь молча терял
        назначенное им время отправки.
        """
        scheduled = NOW + timedelta(hours=5)
        campaign = make_campaign(recipient_emails="", scheduled_time=scheduled)

        with pytest.raises(CampaignNotSendable, match="Нет получателей"):
            _send(db, campaign.id)

        db.expire_all()
        refreshed = db.get(Campaign, campaign.id)
        assert refreshed is not None
        assert refreshed.status == "draft"
        assert refreshed.scheduled_time is not None

    @pytest.mark.regression
    def test_crash_does_not_leave_campaign_sending(
        self, db: Session, make_campaign, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Падение посреди отправки не должно «подвешивать» кампанию навсегда."""

        def explode(_: object) -> None:
            raise RuntimeError("неожиданный сбой")

        monkeypatch.setattr(campaign_sender, "SmtpSession", explode)
        campaign = make_campaign()

        with pytest.raises(RuntimeError):
            _send(db, campaign.id)

        db.expire_all()
        refreshed = db.get(Campaign, campaign.id)
        assert refreshed is not None
        assert refreshed.status == "draft"


class TestCancellation:
    def test_cancel_before_batch_stops_send(
        self, db: Session, make_campaign, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeSmtp()
        monkeypatch.setattr(campaign_sender, "SmtpSession", fake)
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru, c@x.ru, d@x.ru", message_count=1)

        original = campaign_sender._is_cancelled
        calls = {"n": 0}

        def cancel_after_two(session: Session, campaign_id: int) -> bool:
            calls["n"] += 1
            # Отменяем после того, как ушли первые письма.
            if calls["n"] > 3:
                return True
            return original(session, campaign_id)

        monkeypatch.setattr(campaign_sender, "_is_cancelled", cancel_after_two)

        result = _send(db, campaign.id)

        assert result.stopped_by_user
        assert len(fake.sent) < 4
        db.expire_all()
        assert db.get(Campaign, campaign.id).status == "cancelled"  # type: ignore[union-attr]


class TestDailyLimit:
    """Суточный лимит писем на ящик (напр. 1000/день)."""

    def test_no_limit_sends_all(self, db: Session, make_campaign, smtp: FakeSmtp, smtp_profile) -> None:
        smtp_profile.daily_limit = 0
        db.commit()
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru, c@x.ru")

        result = _send(db, campaign.id)

        assert smtp.sent == ["a@x.ru", "b@x.ru", "c@x.ru"]
        assert not result.deferred

    def test_limit_caps_run_and_defers_remainder(
        self, db: Session, make_campaign, smtp: FakeSmtp, smtp_profile
    ) -> None:
        smtp_profile.daily_limit = 2
        db.commit()
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru, c@x.ru")

        result = _send(db, campaign.id)

        # За этот заход ушло только 2 адреса из 3.
        assert smtp.sent == ["a@x.ru", "b@x.ru"]
        assert result.deferred
        db.expire_all()
        refreshed = db.get(Campaign, campaign.id)
        assert refreshed is not None
        # Кампания не «Отправлена», а перенесена на следующие сутки.
        assert refreshed.status == "scheduled"
        assert refreshed.scheduled_time is not None
        # SQLite возвращает время без tz — сравниваем с наивным NOW.
        assert refreshed.scheduled_time.replace(tzinfo=None) > NOW.replace(tzinfo=None)
        # Записан системный лог о переносе.
        deferred = db.execute(
            select(CampaignLog).where(
                CampaignLog.campaign_id == campaign.id, CampaignLog.status == "deferred"
            )
        ).scalars().all()
        assert len(deferred) == 1

    def test_limit_exhausted_defers_without_sending(
        self, db: Session, make_campaign, smtp: FakeSmtp, smtp_profile
    ) -> None:
        smtp_profile.daily_limit = 2
        db.commit()
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru, c@x.ru")
        # Сегодня с этого ящика уже ушло 2 письма (в пределах суток NOW).
        for addr in ("old1@x.ru", "old2@x.ru"):
            db.add(CampaignLog(campaign_id=campaign.id, email=addr, status="sent", created_at=NOW))
        db.commit()

        result = _send(db, campaign.id)

        assert smtp.sent == []
        assert result.deferred
        db.expire_all()
        assert db.get(Campaign, campaign.id).status == "scheduled"  # type: ignore[union-attr]

    def test_yesterday_sends_do_not_count(
        self, db: Session, make_campaign, smtp: FakeSmtp, smtp_profile
    ) -> None:
        smtp_profile.daily_limit = 2
        db.commit()
        campaign = make_campaign(recipient_emails="a@x.ru, b@x.ru")
        # Письмо, отправленное вчера, не должно занимать сегодняшний лимит.
        db.add(CampaignLog(
            campaign_id=campaign.id, email="yst@x.ru", status="sent",
            created_at=NOW - timedelta(days=1),
        ))
        db.commit()

        result = _send(db, campaign.id)

        # Лимит 2, вчерашнее не считается — сегодня уходят оба адреса.
        assert smtp.sent == ["a@x.ru", "b@x.ru"]
        assert not result.deferred


class TestScheduling:
    def test_due_campaign_is_found(self, db: Session, make_campaign) -> None:
        make_campaign(status="scheduled", scheduled_time=NOW - timedelta(minutes=1))
        assert len(find_due_campaigns(db, NOW)) == 1

    def test_future_campaign_is_not_found(self, db: Session, make_campaign) -> None:
        make_campaign(status="scheduled", scheduled_time=NOW + timedelta(hours=1))
        assert find_due_campaigns(db, NOW) == []

    @pytest.mark.regression
    def test_campaign_without_time_is_never_due(self, db: Session, make_campaign) -> None:
        """``scheduled_time IS NULL`` раньше означало «отправить немедленно».

        Пустое поле времени в форме приводило к мгновенной рассылке.
        """
        make_campaign(status="scheduled", scheduled_time=None)
        assert find_due_campaigns(db, NOW) == []

    def test_draft_is_never_due(self, db: Session, make_campaign) -> None:
        make_campaign(status="draft", scheduled_time=NOW - timedelta(hours=1))
        assert find_due_campaigns(db, NOW) == []


class TestZombieRecovery:
    def test_stuck_campaign_is_reset(self, db: Session, make_campaign) -> None:
        campaign = make_campaign(
            status="sending", message_interval=0, updated_at=NOW - timedelta(hours=2)
        )
        recovered = recover_stuck_campaigns(db, NOW)

        assert recovered == [campaign.id]
        db.expire_all()
        assert db.get(Campaign, campaign.id).status == "draft"  # type: ignore[union-attr]

    @pytest.mark.regression
    def test_active_campaign_with_long_interval_is_not_reset(
        self, db: Session, make_campaign
    ) -> None:
        """Порог должен учитывать настроенный интервал между письмами.

        При фиксированном пороге кампания с паузой в 10 минут выглядела
        зависшей и сбрасывалась в черновик прямо посреди рассылки — именно это
        пользователи видели как «рассылка перезапускается сама».
        """
        campaign = make_campaign(
            status="sending",
            message_interval=1800,  # 30 минут между письмами
            updated_at=NOW - timedelta(minutes=20),
        )
        assert recover_stuck_campaigns(db, NOW) == []
        db.expire_all()
        assert db.get(Campaign, campaign.id).status == "sending"  # type: ignore[union-attr]

    def test_recently_active_campaign_is_not_reset(self, db: Session, make_campaign) -> None:
        make_campaign(status="sending", message_interval=0, updated_at=NOW - timedelta(minutes=1))
        assert recover_stuck_campaigns(db, NOW) == []
