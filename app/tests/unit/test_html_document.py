"""Письмо, которое не разъезжается у получателя.

Жалоба была конкретная: на один адрес шаблон приходит нормально, на другой —
«сбежал», с горизонтальной прокруткой и обрезанным по краям текстом. Причина —
таблицы с ``width="900"`` из прежних рассыльщиков: современная вёрстка письма
кладёт содержимое в колонку ~600 px, а жёсткая ширина растягивает собой всё
письмо.

Проверяем именно границу: широкое ослабляем, узкое не трогаем. Иначе лечение
сломает шаблоны, которые сегодня работают.
"""

from __future__ import annotations

import re

from cbmail.domain.composer import compose, html_to_text
from cbmail.domain.html_document import (
    ensure_email_document,
    insert_into_body,
    is_full_document,
    relax_fixed_widths,
)


class TestFixedWidths:
    def test_wide_table_becomes_ceiling(self) -> None:
        out = relax_fixed_widths('<table width="900"><tr><td>текст</td></tr></table>')
        assert "max-width:900px" in out
        assert "width:100%" in out
        assert 'width="900"' not in out

    def test_narrow_width_untouched(self) -> None:
        for width in (600, 560, 320, 1):
            source = f'<table width="{width}"><tr><td>текст</td></tr></table>'
            assert relax_fixed_widths(source) == source, f"ширина {width}"

    def test_percent_untouched(self) -> None:
        source = '<table width="100%"><tr><td width="50%">текст</td></tr></table>'
        assert relax_fixed_widths(source) == source

    def test_inline_width_becomes_ceiling(self) -> None:
        out = relax_fixed_widths('<td style="width:900px;color:red">текст</td>')
        assert "max-width:900px" in out
        assert "color:red" in out

    def test_existing_style_not_replaced_by_second_attribute(self) -> None:
        out = relax_fixed_widths('<table width="900" style="background:#fff">x</table>')
        # Два атрибута style — браузер возьмёт первый и молча выбросит оформление.
        assert out.count("style=") == 1
        assert "background:#fff" in out
        assert "max-width:900px" in out

    def test_image_keeps_width_attribute(self) -> None:
        out = relax_fixed_widths('<img src="a.png" width="880" alt="">')
        assert "max-width:100%" in out
        assert "height:auto" in out
        # Атрибут нужен Outlook — его не убираем.
        assert 'width="880"' in out

    def test_idempotent(self) -> None:
        once = relax_fixed_widths('<table width="900"><tr><td><img src="a.png" width="880"></td></tr></table>')
        assert relax_fixed_widths(once) == once

    def test_empty_and_plain_markup(self) -> None:
        assert relax_fixed_widths("") == ""
        assert relax_fixed_widths("<p>привет</p>") == "<p>привет</p>"


class TestEmailDocument:
    def test_fragment_is_wrapped(self) -> None:
        out = ensure_email_document("<div>привет</div>")
        assert out.lower().startswith("<!doctype html>")
        assert "привет" in out

    def test_empty_head_gets_charset_and_viewport(self) -> None:
        out = ensure_email_document("<!DOCTYPE html><html><head></head><body>x</body></html>")
        assert "charset" in out
        assert "viewport" in out

    def test_idempotent(self) -> None:
        once = ensure_email_document("<div>x</div>")
        assert ensure_email_document(once) == once

    def test_document_without_head(self) -> None:
        out = ensure_email_document("<html><body>x</body></html>")
        assert re.search(r"<head>.*charset.*</head>\s*<body>", out, re.S)

    def test_fragment_and_document_differ(self) -> None:
        assert is_full_document("<div>x</div>") is False
        assert is_full_document("<html><body>x</body></html>") is True


class TestHeaderPlacement:
    def test_inside_body_for_document(self) -> None:
        out = insert_into_body(
            "<html><body><p>письмо</p></body></html>",
            before="<div>шапка</div>",
            after="<div>подвал</div>",
        )
        assert "<body><div>шапка</div><p>письмо</p><div>подвал</div></body>" in out

    def test_around_fragment(self) -> None:
        out = insert_into_body("<p>письмо</p>", before="<i>до</i>", after="<i>после</i>")
        assert out == "<i>до</i><p>письмо</p><i>после</i>"

    def test_no_parts_keeps_markup(self) -> None:
        source = "<html><body>x</body></html>"
        assert insert_into_body(source) == source


class TestComposedMessage:
    DOCUMENT = (
        '<!DOCTYPE html><html><head></head><body>'
        '<table width="900"><tr><td>текст</td></tr></table></body></html>'
    )

    def test_nothing_outside_html(self) -> None:
        message = compose(
            subject="тема",
            body=self.DOCUMENT,
            header="ООО «Гранд Комфорт»",
            footer="Тел. 8-800",
            add_unsubscribe=True,
        )
        # Раньше подвал и ссылка отписки приклеивались после </html>, и строгие
        # почтовые клиенты их выбрасывали вместе с «Отписаться».
        assert not re.search(r"</html>\s*\S", message.html_body or "")
        assert "Отписаться" in (message.html_body or "")
        assert "8-800" in (message.html_body or "")

    def test_widths_relaxed_in_sent_mail(self) -> None:
        message = compose(subject="тема", body=self.DOCUMENT)
        assert "max-width:900px" in (message.html_body or "")

    def test_plain_text_stays_without_html(self) -> None:
        message = compose(subject="тема", body="привет")
        assert message.html_body is None


class TestTextVersion:
    """Без части text/plain письмо выглядит пустым в превью и хуже проходит
    спам-фильтры. Одиночный ``<meta>`` раньше включал режим «не показывать» и
    ничем его не выключал — весь текст после него молча пропадал."""

    def test_content_survives_meta_in_head(self) -> None:
        assert "Привет" in html_to_text(ensure_email_document("<p>Привет</p>"))

    def test_service_markup_not_in_text(self) -> None:
        text = html_to_text(ensure_email_document("<p>Привет</p>"))
        assert "max-width" not in text
        assert "viewport" not in text

    def test_self_closing_meta(self) -> None:
        text = html_to_text('<html><head><meta charset="utf-8"/></head><body><p>Привет</p></body></html>')
        assert "Привет" in text

    def test_styles_stay_invisible_but_text_does_not(self) -> None:
        text = html_to_text(
            "<style>.a{color:red}</style><p>Привет</p><style>.b{color:blue}</style><p>Пока</p>"
        )
        assert "color:red" not in text
        assert "color:blue" not in text
        assert "Привет" in text
        assert "Пока" in text
