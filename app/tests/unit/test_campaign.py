"""Тесты правил жизненного цикла кампании."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cbmail.domain.campaign import (
    CampaignStatus,
    InvalidTransition,
    SendPlan,
    build_send_plan,
    can_transition,
    clamp_batch_size,
    clamp_interval,
    ensure_transition,
    is_due,
)

UTC = timezone.utc


class TestTransitions:
    def test_draft_can_be_scheduled(self) -> None:
        assert can_transition(CampaignStatus.DRAFT, CampaignStatus.SCHEDULED)

    def test_scheduled_can_be_cancelled(self) -> None:
        assert can_transition(CampaignStatus.SCHEDULED, CampaignStatus.CANCELLED)

    def test_sending_can_finish(self) -> None:
        assert can_transition(CampaignStatus.SENDING, CampaignStatus.SENT)

    def test_cancelled_can_be_resumed(self) -> None:
        assert can_transition(CampaignStatus.CANCELLED, CampaignStatus.SCHEDULED)

    def test_same_status_is_noop(self) -> None:
        assert can_transition(CampaignStatus.SENT, CampaignStatus.SENT)

    @pytest.mark.regression
    def test_sent_cannot_go_back_to_scheduled(self) -> None:
        """Отправленную кампанию нельзя перепланировать напрямую.

        В Django-версии статус присваивался прямым `campaign.status = ...`
        без проверок, поэтому повторный запуск уже отправленной кампании
        приводил к дублю рассылки по всей базе получателей.
        """
        assert not can_transition(CampaignStatus.SENT, CampaignStatus.SCHEDULED)
        assert not can_transition(CampaignStatus.SENT, CampaignStatus.SENDING)

    @pytest.mark.regression
    def test_draft_cannot_jump_to_sent(self) -> None:
        assert not can_transition(CampaignStatus.DRAFT, CampaignStatus.SENT)

    def test_ensure_transition_raises_with_readable_message(self) -> None:
        with pytest.raises(InvalidTransition) as exc:
            ensure_transition(CampaignStatus.SENT, CampaignStatus.SENDING)
        assert "Отправлена" in str(exc.value)
        assert "Отправляется" in str(exc.value)


class TestClamping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0, 1), (1, 1), (50, 50), (1000, 1000), (5000, 1000), (-10, 1)],
    )
    def test_batch_size_bounds(self, raw: int, expected: int) -> None:
        assert clamp_batch_size(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0, 0), (2, 2), (600, 600), (86_400, 86_400), (999_999, 86_400), (-5, 0)],
    )
    def test_interval_bounds(self, raw: int, expected: int) -> None:
        assert clamp_interval(raw) == expected

    @pytest.mark.regression
    def test_interval_above_one_minute_is_preserved(self) -> None:
        """Раньше интервал жёстко ограничивался 60 секундами.

        Кампания, настроенная на паузу 10 минут, молча превращалась в паузу
        в одну минуту — рассылка уходила в 10 раз быстрее задуманного.
        """
        assert clamp_interval(600) == 600

    @pytest.mark.regression
    def test_fractional_string_does_not_reset_both_params(self) -> None:
        """``int("2.5")`` возбуждал ValueError, общий ``except`` сбрасывал ОБА
        параметра на дефолт — пользователь терял и размер пачки, и интервал."""
        assert clamp_interval("2.5") == 2
        assert clamp_batch_size("100.0") == 100

    def test_comma_decimal_separator(self) -> None:
        assert clamp_interval("2,7") == 2

    def test_garbage_falls_back_to_default(self) -> None:
        assert clamp_interval("абв") == 2
        assert clamp_batch_size(None) == 50


class TestIsDue:
    def test_past_time_is_due(self) -> None:
        now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        assert is_due(now - timedelta(minutes=5), now)

    def test_future_time_is_not_due(self) -> None:
        now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        assert not is_due(now + timedelta(minutes=5), now)

    def test_within_tolerance_is_due(self) -> None:
        now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        assert is_due(now + timedelta(seconds=10), now)

    @pytest.mark.regression
    def test_none_is_never_due(self) -> None:
        """``scheduled_time IS NULL`` раньше трактовалось как «отправить сейчас».

        Пустое поле времени в форме приводило к немедленной рассылке вместо
        сохранения черновика — пользователь получал «отправилось само».
        """
        now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        assert not is_due(None, now)

    @pytest.mark.regression
    def test_naive_datetime_is_rejected_loudly(self) -> None:
        """Наивное время молча сравнивалось и давало сдвиг на часы поясов."""
        now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="timezone-aware"):
            is_due(datetime(2026, 7, 31, 11, 0), now)


class TestBuildSendPlan:
    def test_simple_plan(self) -> None:
        plan = build_send_plan(
            raw_recipients="a@x.ru, b@x.ru, c@x.ru",
            global_stoplist=set(),
            campaign_stoplist=None,
            already_sent=set(),
        )
        assert plan.recipients == ("a@x.ru", "b@x.ru", "c@x.ru")
        assert plan.total == 3
        assert plan.already_sent == 0
        assert not plan.is_empty

    def test_global_stoplist_is_applied(self) -> None:
        plan = build_send_plan(
            raw_recipients="a@x.ru, b@x.ru",
            global_stoplist={"b@x.ru"},
            campaign_stoplist=None,
            already_sent=set(),
        )
        assert plan.recipients == ("a@x.ru",)
        assert plan.skipped_stoplist == 1

    def test_campaign_stoplist_is_applied(self) -> None:
        plan = build_send_plan(
            raw_recipients="a@x.ru, b@x.ru",
            global_stoplist=set(),
            campaign_stoplist="a@x.ru",
            already_sent=set(),
        )
        assert plan.recipients == ("b@x.ru",)
        assert plan.skipped_stoplist == 1

    @pytest.mark.regression
    def test_stoplist_matching_is_case_insensitive(self) -> None:
        """Стоп-лист и список получателей сравнивались по-разному."""
        plan = build_send_plan(
            raw_recipients="Ivan@Mail.ru",
            global_stoplist={"ivan@mail.ru"},
            campaign_stoplist=None,
            already_sent=set(),
        )
        assert plan.is_empty

    @pytest.mark.regression
    def test_already_sent_matching_is_case_insensitive(self) -> None:
        """``already_sent`` сравнивался точным совпадением, а стоп-лист — нет.

        При возобновлении кампании адрес, записанный в лог в другом регистре,
        получал письмо повторно.
        """
        plan = build_send_plan(
            raw_recipients="Ivan@Mail.ru, petr@x.ru",
            global_stoplist=set(),
            campaign_stoplist=None,
            already_sent={"ivan@mail.ru"},
        )
        assert plan.recipients == ("petr@x.ru",)
        assert plan.already_sent == 1

    @pytest.mark.regression
    def test_duplicates_get_one_email_each(self) -> None:
        plan = build_send_plan(
            raw_recipients="a@x.ru, a@x.ru, A@X.RU",
            global_stoplist=set(),
            campaign_stoplist=None,
            already_sent=set(),
        )
        assert plan.recipients == ("a@x.ru",)
        assert plan.total == 1

    def test_invalid_recipients_are_counted_not_sent(self) -> None:
        plan = build_send_plan(
            raw_recipients="good@x.ru, мусор, @",
            global_stoplist=set(),
            campaign_stoplist=None,
            already_sent=set(),
        )
        assert plan.recipients == ("good@x.ru",)
        assert plan.skipped_invalid == 2

    def test_fully_sent_campaign_is_complete_not_failed(self) -> None:
        """Возобновление кампании, где всем уже отправлено, — это успех."""
        plan = build_send_plan(
            raw_recipients="a@x.ru",
            global_stoplist=set(),
            campaign_stoplist=None,
            already_sent={"a@x.ru"},
        )
        assert plan.is_empty
        assert plan.is_complete

    def test_empty_campaign_is_not_complete(self) -> None:
        plan = build_send_plan(
            raw_recipients="",
            global_stoplist=set(),
            campaign_stoplist=None,
            already_sent=set(),
        )
        assert plan.is_empty
        assert not plan.is_complete

    def test_stoplist_after_send_is_not_double_counted(self) -> None:
        """Адрес, попавший в стоп-лист после отправки, считается отправленным.

        Порядок фильтров зафиксирован: стоп-лист применяется до вычитания
        уже отправленных, поэтому такой адрес не попадает в обе категории.
        """
        plan = build_send_plan(
            raw_recipients="a@x.ru, b@x.ru",
            global_stoplist={"a@x.ru"},
            campaign_stoplist=None,
            already_sent={"a@x.ru"},
        )
        assert plan.recipients == ("b@x.ru",)
        assert plan.skipped_stoplist == 1
        assert plan.already_sent == 0
        assert plan.total == 1


class TestBatching:
    def test_splits_into_batches(self) -> None:
        plan = SendPlan(
            recipients=tuple(f"u{i}@x.ru" for i in range(10)),
            total=10,
            already_sent=0,
            skipped_stoplist=0,
            skipped_invalid=0,
        )
        batches = plan.batches(3)
        assert [len(b) for b in batches] == [3, 3, 3, 1]
        assert batches[0] == ("u0@x.ru", "u1@x.ru", "u2@x.ru")

    def test_batch_size_is_clamped(self) -> None:
        plan = SendPlan(
            recipients=("a@x.ru", "b@x.ru"),
            total=2,
            already_sent=0,
            skipped_stoplist=0,
            skipped_invalid=0,
        )
        assert len(plan.batches(0)) == 2  # 0 → 1 письмо на батч

    def test_empty_plan_yields_no_batches(self) -> None:
        plan = SendPlan(
            recipients=(), total=0, already_sent=0, skipped_stoplist=0, skipped_invalid=0
        )
        assert plan.batches(50) == []
