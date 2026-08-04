"""Тесты домена email-адресов.

Каждый тест, помеченный ``regression``, соответствует конкретному дефекту
Django-версии — см. docs/BUGS.md.
"""

from __future__ import annotations

import pytest

from cbmail.domain import emails


class TestIsValidEmail:
    @pytest.mark.parametrize(
        "value",
        [
            "user@example.com",
            "user.name@example.com",
            "user+tag@example.co.uk",
            "user_name@sub.example.com",
            "a@b.io",
            "user-name@example-site.com",
            "1234@example.com",
        ],
    )
    def test_accepts_valid(self, value: str) -> None:
        assert emails.is_valid_email(value)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "@",
            "user@",
            "@example.com",
            "user@example",  # нет TLD
            "user@@example.com",
            "user name@example.com",
            ".user@example.com",
            "user.@example.com",
            "us..er@example.com",
            "user@example..com",
            "user@-example.com",
            "user@example.c",  # TLD короче двух символов
        ],
    )
    def test_rejects_invalid(self, value: str) -> None:
        assert not emails.is_valid_email(value)

    def test_rejects_too_long(self) -> None:
        long_local = "a" * 250
        assert not emails.is_valid_email(f"{long_local}@example.com")

    def test_rejects_too_long_local_part(self) -> None:
        local = "a" * 65
        assert not emails.is_valid_email(f"{local}@example.com")

    @pytest.mark.regression
    def test_at_sign_alone_is_not_an_email(self) -> None:
        """Старая проверка была ``'@' in part`` — эти токены проходили её.

        В результате в получатели кампании попадал мусор, каждая попытка
        отправки на него падала и раздувала failed_count.
        """
        for junk in ["@", "a@b", "--@--", "http://site.ru/?a=b@c"]:
            assert not emails.is_valid_email(junk)


class TestParseEmails:
    def test_splits_on_comma_newline_space_semicolon(self) -> None:
        raw = "a@x.ru, b@x.ru\nc@x.ru d@x.ru;e@x.ru"
        assert emails.parse_emails(raw) == [
            "a@x.ru",
            "b@x.ru",
            "c@x.ru",
            "d@x.ru",
            "e@x.ru",
        ]

    @pytest.mark.regression
    def test_semicolon_separated_outlook_paste(self) -> None:
        """Старый разделитель ``[,\\n\\s]+`` не знал про ';'.

        Вставка списка из Outlook давала один склеенный токен
        ``a@x.ru;b@x.ru``, который затем целиком уходил в поле ``to``.
        """
        assert emails.parse_emails("a@x.ru;b@x.ru") == ["a@x.ru", "b@x.ru"]

    def test_preserves_order_of_first_appearance(self) -> None:
        raw = "zeta@x.ru, alpha@x.ru, mid@x.ru"
        assert emails.parse_emails(raw) == ["zeta@x.ru", "alpha@x.ru", "mid@x.ru"]

    @pytest.mark.regression
    def test_deduplicates_case_insensitively(self) -> None:
        """``MassMailCampaign.get_recipients_list`` не дедуплицировал вовсе.

        Адрес, указанный дважды (частый случай при склейке файла и ручного
        ввода), получал два письма.
        """
        raw = "Ivan@Mail.ru, ivan@mail.ru, IVAN@MAIL.RU"
        assert emails.parse_emails(raw) == ["Ivan@Mail.ru"]

    def test_drops_invalid_tokens(self) -> None:
        assert emails.parse_emails("good@x.ru, невалид, @, bad@") == ["good@x.ru"]

    def test_empty_input(self) -> None:
        assert emails.parse_emails("") == []
        assert emails.parse_emails(None) == []

    def test_strips_angle_brackets(self) -> None:
        assert emails.parse_emails("<user@x.ru>") == ["user@x.ru"]


class TestSplitEmails:
    def test_reports_invalid_separately(self) -> None:
        valid, invalid = emails.split_emails("ok@x.ru, мусор, also-bad")
        assert valid == ["ok@x.ru"]
        assert invalid == ["мусор", "also-bad"]

    def test_duplicates_are_not_reported_as_invalid(self) -> None:
        valid, invalid = emails.split_emails("a@x.ru, a@x.ru")
        assert valid == ["a@x.ru"]
        assert invalid == []


class TestExclude:
    @pytest.mark.regression
    def test_excludes_case_insensitively(self) -> None:
        """Стоп-лист сравнивался регистрозависимо при добавлении.

        ``Ivan@mail.ru`` в стоп-листе не блокировал ``ivan@mail.ru``.
        """
        result = emails.exclude(["ivan@mail.ru", "petr@mail.ru"], ["IVAN@MAIL.RU"])
        assert result == ["petr@mail.ru"]

    def test_preserves_order(self) -> None:
        result = emails.exclude(["c@x.ru", "a@x.ru", "b@x.ru"], ["a@x.ru"])
        assert result == ["c@x.ru", "b@x.ru"]

    def test_empty_exclusion_returns_all(self) -> None:
        assert emails.exclude(["a@x.ru"], []) == ["a@x.ru"]


class TestJoinEmails:
    @pytest.mark.regression
    def test_join_is_order_stable(self) -> None:
        """``', '.join(set(...))`` в ``move_emails_to_stoplist`` терял порядок.

        Порядок получателей менялся при каждом сохранении кампании, из-за чего
        возобновление рассылки шло не с того места.
        """
        source = ["b@x.ru", "a@x.ru", "c@x.ru"]
        assert emails.join_emails(source) == "b@x.ru, a@x.ru, c@x.ru"
        # Повторный проход через parse/join не меняет порядок.
        assert emails.parse_emails(emails.join_emails(source)) == source

    def test_roundtrip(self) -> None:
        source = "a@x.ru, b@x.ru"
        assert emails.join_emails(emails.parse_emails(source)) == source
