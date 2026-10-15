"""Письмо уходит к получателю с той вёрсткой, какую сделал автор шаблона.

История здесь поучительная. Сначала жалоба звучала как «шаблон разъезжается», и
письмо стали «делать отзывчивым» на лету: жёсткие ширины превращались в потолок,
в ``<head>`` добавлялись viewport и правила, ужимающие таблицы.

Стало хуже. Меню «о компании · каталог · новости» на телефоне превратилось в три
столбика букв по вертикали, а на ноутбуке текст расползся по всей ширине и
наехал на картинки. Причины, каждая из которых сама по себе достаточна:

* **viewport** переключает мобильный клиент из режима «нарисовать как на
  десктопе и уменьшить целиком» в режим «рисовать по ширине телефона». Шаблон
  без медиазапросов к этому не готов: таблицы начинают ужиматься колонками;
* **word-break: break-word** на ячейках разрешает рвать слова где угодно.
  Минимальная ширина колонки становится равна одному символу — отсюда буквы
  столбиком;
* **width:100%** рядом с ``max-width`` растягивает таблицу на всю ширину окна.

Поэтому правило теперь обратное: вёрстку не трогаем. Эти тесты стерегут именно
его — любая правка, которая снова начнёт «улучшать» раскладку, их уронит.
"""

from __future__ import annotations

import re

from cbmail.domain.composer import compose, html_to_text
from cbmail.domain.html_document import (
    cap_images,
    ensure_email_document,
    insert_into_body,
    is_full_document,
    is_responsive,
)

# Меню из трёх пунктов — то самое, что рассыпалось на буквы.
NAV = (
    '<table width="600" cellpadding="0" cellspacing="0"><tr>'
    '<td width="200">о компании</td><td width="200">каталог</td>'
    '<td width="200">новости</td></tr></table>'
)


class TestLayoutUntouched:
    """Ширины шаблона остаются ровно такими, какими их задал автор."""

    def test_table_width_attribute_survives(self) -> None:
        out = ensure_email_document(cap_images(NAV))
        assert 'width="600"' in out
        assert 'width="200"' in out

    def test_wide_table_not_relaxed(self) -> None:
        # Таблица на 900 px — осознанный выбор автора, а не ошибка.
        out = ensure_email_document(cap_images('<table width="900"><tr><td>текст</td></tr></table>'))
        assert 'width="900"' in out
        assert "max-width:900px" not in out

    def test_no_width_100_percent_added(self) -> None:
        out = ensure_email_document(cap_images(NAV))
        # Именно самостоятельное width, не хвост от max-width у картинок:
        # растягивание таблицы по окну и есть то, из-за чего текст наезжал на
        # картинки на широком экране.
        assert not re.search(r"(?<!max-)width\s*:\s*100%", out)

    def test_cells_do_not_break_words(self) -> None:
        out = ensure_email_document(cap_images(NAV))
        assert "word-break" not in out, "от этого меню рассыпается на буквы столбиком"

    def test_tables_not_squeezed(self) -> None:
        out = ensure_email_document(cap_images(NAV))
        assert "table{max-width" not in out.replace(" ", "")

    def test_inline_styles_untouched(self) -> None:
        out = cap_images('<table style="width:900px;background:#fff"><tr><td>т</td></tr></table>')
        assert "width:900px" in out
        assert "max-width" not in out


class TestViewport:
    """Viewport — только тем шаблонам, которые к нему готовы."""

    def test_absent_for_fixed_layout(self) -> None:
        out = ensure_email_document(NAV)
        assert "viewport" not in out, (
            "без медиазапросов viewport ломает фиксированную вёрстку: "
            "клиент перестаёт уменьшать письмо целиком"
        )

    def test_present_for_responsive_template(self) -> None:
        responsive = (
            "<html><head><style>@media(max-width:480px){.c{width:100%!important}}</style></head>"
            f"<body>{NAV}</body></html>"
        )
        assert is_responsive(responsive)
        out = ensure_email_document(responsive)
        assert "viewport" in out, "медиазапросы без viewport не сработают вовсе"

    def test_not_duplicated(self) -> None:
        responsive = (
            '<html><head><meta name="viewport" content="width=device-width">'
            "<style>@media(max-width:480px){.c{width:100%}}</style></head><body>т</body></html>"
        )
        assert ensure_email_document(responsive).lower().count("viewport") == 1


class TestImages:
    """Единственное вмешательство в разметку — потолок для картинки."""

    def test_image_keeps_width_attribute(self) -> None:
        out = cap_images('<img src="a.png" width="1200">')
        assert 'width="1200"' in out, "на атрибут опирается Outlook"
        assert "max-width:100%" in out

    def test_existing_style_not_replaced_by_second_attribute(self) -> None:
        out = cap_images('<img src="a.png" style="border:0">')
        assert out.count("style=") == 1
        assert "border:0" in out
        assert "max-width:100%" in out

    def test_idempotent(self) -> None:
        once = cap_images('<img src="a.png" width="1200">')
        assert cap_images(once) == once

    def test_empty_and_plain_markup(self) -> None:
        assert cap_images("") == ""
        assert cap_images("<p>привет</p>") == "<p>привет</p>"


class TestDocument:
    def test_fragment_is_wrapped(self) -> None:
        out = ensure_email_document("<p>привет</p>")
        assert is_full_document(out)
        assert "charset" in out

    def test_empty_head_gets_charset(self) -> None:
        out = ensure_email_document("<html><head></head><body><p>т</p></body></html>")
        assert "charset" in out

    def test_idempotent(self) -> None:
        once = ensure_email_document("<p>привет</p>")
        assert ensure_email_document(once) == once

    def test_document_without_head(self) -> None:
        out = ensure_email_document("<html><body><p>т</p></body></html>")
        assert "<head>" in out and "charset" in out

    def test_fragment_and_document_differ(self) -> None:
        assert not is_full_document("<p>т</p>")
        assert is_full_document("<html><body>т</body></html>")


class TestInsertIntoBody:
    def test_inside_body_for_document(self) -> None:
        out = insert_into_body(
            "<html><body><p>т</p></body></html>", before="<i>шапка</i>", after="<i>подвал</i>"
        )
        assert "<body><i>шапка</i>" in out
        assert "<i>подвал</i></body>" in out

    def test_around_fragment(self) -> None:
        out = insert_into_body("<p>т</p>", before="ш", after="п")
        assert out == "ш<p>т</p>п"

    def test_no_parts_keeps_markup(self) -> None:
        assert insert_into_body("<p>т</p>") == "<p>т</p>"


class TestComposedMessage:
    DOCUMENT = f"<!DOCTYPE html><html><head></head><body>{NAV}</body></html>"

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

    def test_layout_survives_sending(self) -> None:
        message = compose(subject="тема", body=self.DOCUMENT)
        html = message.html_body or ""
        assert 'width="600"' in html and 'width="200"' in html
        assert "viewport" not in html
        assert "word-break" not in html

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
        assert "charset" not in text

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
