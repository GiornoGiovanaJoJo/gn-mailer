"""Тесты сборки тела письма."""

from __future__ import annotations

import pytest

from cbmail.domain import composer


class TestHtmlToText:
    @pytest.mark.regression
    def test_style_content_is_not_leaked_into_text(self) -> None:
        """``strip_tags`` вываливал CSS прямо в текст письма.

        Получатель текстового клиента (и превью в списке писем) видел
        ``body{margin:0}`` вместо первых слов сообщения.
        """
        html = "<html><head><style>body{margin:0;padding:10px}</style></head><body>Привет</body></html>"
        text = composer.html_to_text(html)
        assert text == "Привет"
        assert "margin" not in text

    @pytest.mark.regression
    def test_script_content_is_not_leaked(self) -> None:
        html = "<body><script>var a = 1;</script>Текст</body>"
        assert composer.html_to_text(html) == "Текст"

    @pytest.mark.regression
    def test_adjacent_cells_are_not_glued_together(self) -> None:
        """``strip_tags`` склеивал соседние ячейки: «ИмяФамилия»."""
        html = "<table><tr><td>Имя</td><td>Фамилия</td></tr></table>"
        text = composer.html_to_text(html)
        assert "ИмяФамилия" not in text
        assert "Имя" in text and "Фамилия" in text

    @pytest.mark.regression
    def test_links_keep_their_target(self) -> None:
        """Без адреса ссылки текст «нажмите здесь» терял смысл."""
        html = '<p>Чтобы отписаться, <a href="https://site.ru/unsubscribe/">нажмите здесь</a></p>'
        text = composer.html_to_text(html)
        assert "нажмите здесь" in text
        assert "https://site.ru/unsubscribe/" in text

    def test_image_alt_is_preserved(self) -> None:
        html = '<img src="logo.png" alt="Логотип компании">'
        assert "Логотип компании" in composer.html_to_text(html)

    def test_entities_are_decoded(self) -> None:
        assert composer.html_to_text("<p>&laquo;Тест&raquo; &amp; K&deg;</p>") == "«Тест» & K°"

    def test_paragraphs_are_separated_by_blank_line(self) -> None:
        html = "<p>Первый</p><p>Второй</p>"
        assert composer.html_to_text(html) == "Первый\n\nВторой"

    def test_br_makes_single_newline(self) -> None:
        assert composer.html_to_text("Строка1<br>Строка2") == "Строка1\nСтрока2"

    def test_excessive_blank_lines_are_collapsed(self) -> None:
        html = "<div><p>А</p></div><div><p>Б</p></div>"
        assert "\n\n\n" not in composer.html_to_text(html)

    def test_anchors_and_javascript_are_not_shown(self) -> None:
        html = '<a href="#top">наверх</a> <a href="javascript:void(0)">клик</a>'
        text = composer.html_to_text(html)
        assert "#top" not in text
        assert "javascript" not in text

    def test_empty_input(self) -> None:
        assert composer.html_to_text("") == ""

    def test_broken_html_does_not_raise(self) -> None:
        """Некорректная разметка не должна срывать отправку всей кампании."""
        assert composer.html_to_text("<p>Текст<<>><div") is not None


class TestLooksLikeHtml:
    @pytest.mark.parametrize(
        "value", ["<p>текст</p>", "<div>a</div>", "<TABLE><tr></tr></TABLE>", "<br>"]
    )
    def test_detects_html(self, value: str) -> None:
        assert composer.looks_like_html(value)

    @pytest.mark.parametrize(
        "value", ["обычный текст", "цена < 100 и > 50", "", "a@b.ru пишет: 5<6"]
    )
    def test_detects_plain_text(self, value: str) -> None:
        assert not composer.looks_like_html(value)


class TestCompose:
    def test_plain_text_stays_plain(self) -> None:
        result = composer.compose(subject="Тема", body="Просто текст")
        assert result.html_body is None
        assert result.text_body == "Просто текст"

    @pytest.mark.regression
    def test_plain_text_keeps_line_breaks(self) -> None:
        """Раньше текст всегда прогонялся через HTML-шаблон и терял переносы."""
        body = "Первая строка\nВторая строка"
        result = composer.compose(subject="Тема", body=body)
        assert result.text_body == body

    def test_header_and_footer_are_added_to_plain_text(self) -> None:
        result = composer.compose(
            subject="Тема", body="Тело", header="Здравствуйте!", footer="С уважением"
        )
        assert result.text_body == "Здравствуйте!\n\nТело\n\nС уважением"

    def test_html_body_gets_both_versions(self) -> None:
        result = composer.compose(subject="Тема", body="<p>Тело</p>")
        assert result.html_body is not None
        assert result.text_body == "Тело"

    @pytest.mark.regression
    def test_footer_with_angle_bracket_does_not_break_layout(self) -> None:
        """Подпись вида ``<ООО Ромашка>`` раньше уезжала в разметку как тег."""
        result = composer.compose(
            subject="Тема", body="<p>Тело</p>", footer="<ООО Ромашка>"
        )
        assert result.html_body is not None
        assert "&lt;ООО Ромашка&gt;" in result.html_body

    def test_empty_header_footer_add_nothing(self) -> None:
        result = composer.compose(subject="Т", body="Тело", header="   ", footer=None)
        assert result.text_body == "Тело"

    def test_explicit_html_flag_is_respected(self) -> None:
        result = composer.compose(subject="Т", body="просто текст", body_is_html=True)
        assert result.html_body is not None


class TestPersonalize:
    def test_substitutes_email_placeholder(self) -> None:
        message = composer.compose(
            subject="Тема", body="Отписка: https://s.ru/unsubscribe/{{email}}/"
        )
        result = message.personalize("user@x.ru")
        assert "https://s.ru/unsubscribe/user@x.ru/" in result.text_body

    def test_substitutes_in_subject(self) -> None:
        message = composer.compose(subject="Письмо для {{email}}", body="Тело")
        assert message.personalize("a@x.ru").subject == "Письмо для a@x.ru"

    def test_substitutes_in_html(self) -> None:
        message = composer.compose(subject="Т", body='<a href="{{email}}">x</a>')
        result = message.personalize("a@x.ru")
        assert result.html_body is not None
        assert "a@x.ru" in result.html_body

    def test_unsubscribe_url_placeholder(self) -> None:
        message = composer.compose(subject="Т", body="Отписаться: {{unsubscribe_url}}")
        result = message.personalize("a@x.ru", "https://s.ru/unsubscribe/a%40x.ru/")
        assert "https://s.ru/unsubscribe/a%40x.ru/" in result.text_body

    def test_original_message_is_not_mutated(self) -> None:
        """Тело собирается один раз на кампанию и переиспользуется для всех."""
        message = composer.compose(subject="Т", body="{{email}}")
        message.personalize("first@x.ru")
        second = message.personalize("second@x.ru")
        assert second.text_body == "second@x.ru"


class TestUnsubscribeUrl:
    @pytest.mark.regression
    def test_plus_in_local_part_is_encoded(self) -> None:
        """Незакодированный ``+`` в пути ломал разбор адреса при отписке."""
        url = composer.build_unsubscribe_url("https://s.ru", "user+tag@x.ru")
        assert "user%2Btag%40x.ru" in url
        assert "+" not in url.rsplit("/", 2)[-2]

    def test_at_sign_is_encoded(self) -> None:
        # Адрес уходит в query-параметр ``e`` (символ @ в пути ненадёжен за nginx).
        url = composer.build_unsubscribe_url("https://s.ru", "a@x.ru")
        assert url == "https://s.ru/unsubscribe?e=a%40x.ru"

    def test_trailing_slash_in_base_is_handled(self) -> None:
        assert composer.build_unsubscribe_url(
            "https://s.ru/", "a@x.ru"
        ) == "https://s.ru/unsubscribe?e=a%40x.ru"

    def test_scope_domain_is_added_as_param(self) -> None:
        """Домен отправителя делает отписку адресной (параметр ``d``)."""
        url = composer.build_unsubscribe_url("https://s.ru", "a@x.ru", scope="pmmarketing.ru")
        assert url == "https://s.ru/unsubscribe?e=a%40x.ru&d=pmmarketing.ru"

    def test_empty_scope_keeps_url_backward_compatible(self) -> None:
        url = composer.build_unsubscribe_url("https://s.ru", "a@x.ru", scope="")
        assert url == "https://s.ru/unsubscribe?e=a%40x.ru"


class TestComposeUnsubscribe:
    """Ссылка отписки добавляется автоматически (``add_unsubscribe=True``)."""

    def test_plain_text_gets_clickable_html_link(self) -> None:
        """Из простого текста строится HTML-версия: «Отписаться» — слово-ссылка."""
        result = composer.compose(
            subject="Т", body="Привет, друзья!", add_unsubscribe=True
        )
        # Текстовая версия — с адресом ссылки рядом (для клиентов без HTML).
        assert composer.UNSUBSCRIBE_PLACEHOLDER in result.text_body
        assert "Отписаться" in result.text_body
        # HTML-версия — кликабельное слово «Отписаться».
        assert result.html_body is not None
        assert f'<a href="{composer.UNSUBSCRIBE_PLACEHOLDER}"' in result.html_body
        assert ">Отписаться</a>" in result.html_body

    def test_clickable_link_after_personalization(self) -> None:
        result = composer.compose(
            subject="Т", body="Текст письма", add_unsubscribe=True
        ).personalize("a@x.ru", "https://s.ru/unsubscribe?e=a%40x.ru&d=pm.ru")
        assert result.html_body is not None
        assert '<a href="https://s.ru/unsubscribe?e=a%40x.ru&d=pm.ru"' in result.html_body
        assert ">Отписаться</a>" in result.html_body

    def test_html_body_gets_html_footer(self) -> None:
        result = composer.compose(
            subject="Т", body="<p>Здравствуйте</p>", add_unsubscribe=True
        )
        assert result.html_body is not None
        assert "<a href=" in result.html_body and "Отписаться" in result.html_body

    def test_manual_placeholder_is_not_duplicated(self) -> None:
        body = "Текст с ссылкой {{unsubscribe_url}} внутри."
        result = composer.compose(subject="Т", body=body, add_unsubscribe=True)
        assert result.text_body.count(composer.UNSUBSCRIBE_PLACEHOLDER) == 1

    def test_disabled_by_default(self) -> None:
        """Без ``add_unsubscribe`` (одиночные письма) подвал не добавляется."""
        result = composer.compose(subject="Т", body="Привет")
        assert composer.UNSUBSCRIBE_PLACEHOLDER not in result.text_body
        assert result.html_body is None


class TestCombineBody:
    """Шаблон и собственный текст: любое сочетание из трёх."""

    def test_only_template(self) -> None:
        assert composer.combine_body("<p>Шаблон</p>", "") == "<p>Шаблон</p>"

    def test_only_template_when_text_is_blank_spaces(self) -> None:
        assert composer.combine_body("<p>Шаблон</p>", "   ") == "<p>Шаблон</p>"

    def test_only_text(self) -> None:
        assert composer.combine_body("", "Мой текст") == "Мой текст"
        assert composer.combine_body(None, "Мой текст") == "Мой текст"

    def test_both_empty(self) -> None:
        assert composer.combine_body("", "") == ""

    def test_text_goes_above_html_template(self) -> None:
        out = composer.combine_body("<p>Шаблон</p>", "Здравствуйте!")
        # Текст пользователя — выше оформления шаблона.
        assert out.index("Здравствуйте!") < out.index("<p>Шаблон</p>")
        # Простой текст обёрнут в HTML, чтобы не сломать вёрстку шаблона.
        assert "Здравствуйте!" in out

    def test_both_plain_text_joined(self) -> None:
        out = composer.combine_body("Тело шаблона", "Мой текст")
        assert out == "Мой текст\n\nТело шаблона"

    def test_combined_html_composes_and_adds_unsubscribe(self) -> None:
        """Итог объединения корректно проходит через compose с отпиской."""
        body = composer.combine_body("<p>Промо</p>", "Привет, {{email}}!")
        result = composer.compose(subject="Т", body=body, add_unsubscribe=True)
        assert result.html_body is not None
        assert "Привет" in result.html_body and "Промо" in result.html_body
        assert ">Отписаться</a>" in result.html_body
