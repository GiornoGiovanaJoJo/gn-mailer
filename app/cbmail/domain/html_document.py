"""Подготовка HTML письма к отправке.

Здесь решаются две вещи, из-за которых вёрстка «разъезжалась» у получателя.

Первая — фиксированные ширины. Шаблоны, сделанные под прежние рассыльщики,
свёрстаны таблицами с ``width="900"``: там письмо отдавалось во всю ширину
окна, и это сходило с рук. Современная вёрстка писем кладёт содержимое в
колонку ~600 px, и таблица с жёсткой шириной растягивает собой всё письмо — у
получателя появляется горизонтальная прокрутка, а текст обрезается по краям.
Ширины больше ширины письма превращаются в потолок: ``max-width`` вместо
``width``. На широком экране письмо выглядит ровно как раньше, на узком —
сжимается вместо того, чтобы уехать вбок.

Вторая — место для шапки, подвала и ссылки отписки. Редактор отдаёт
законченный документ с ``<html>``, а они приклеивались снаружи: часть письма
оказывалась после ``</html>``. Строгие почтовые клиенты такое выбрасывают — и
вместе с подвалом пропадала ссылка «Отписаться», ради которой всё и
задумывалось. Теперь всё кладётся внутрь ``<body>``.

Разбор регулярными выражениями, а не парсером: трогаем только атрибуты ширины
в открывающих тегах, и полноценное дерево ради этого тянуть незачем.
Неизвестную разметку функции оставляют как есть.
"""

from __future__ import annotations

import re

# Ширина колонки письма. Всё, что шире, — подозрительно.
EMAIL_WIDTH = 600

_DOCUMENT_RE = re.compile(r"<html[\s>]", re.IGNORECASE)
_HEAD_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)
_CHARSET_RE = re.compile(r"<meta[^>]+charset", re.IGNORECASE)

_TABLE_TAG_RE = re.compile(r"<(?:table|td|th)\b[^>]*>", re.IGNORECASE)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_WIDTH_ATTR_RE = re.compile(r"\swidth\s*=\s*[\"']?(\d+)(?:px)?[\"']?", re.IGNORECASE)
_STYLE_ATTR_RE = re.compile(r"\sstyle\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
_CSS_WIDTH_RE = re.compile(r"(^|;)(\s*)width\s*:\s*(\d+)px", re.IGNORECASE)
_TAG_END_RE = re.compile(r"\s*(/?)>$")


def is_full_document(html: str) -> bool:
    """Есть ли в строке полноценный документ, а не фрагмент разметки."""
    return bool(_DOCUMENT_RE.search(html or ""))


def _with_style(tag_source: str, extra: str) -> str:
    """Добавить или дополнить значение ``style`` у открывающего тега.

    Отдельной функцией, потому что наивная вставка второго атрибута ``style``
    молча отменяет первый: браузер берёт начальный и игнорирует остальные — так
    можно потерять всё оформление тега, пытаясь ограничить ему ширину.
    """
    match = _STYLE_ATTR_RE.search(tag_source)
    if match:
        quote, css = match.group(1), match.group(2)
        trimmed = css.strip().rstrip(";")
        merged = f"{trimmed};{extra}" if trimmed else extra
        return tag_source[: match.start()] + f" style={quote}{merged}{quote}" + tag_source[match.end() :]
    # Вставляем перед закрывающей скобкой, сохраняя самозакрывающий слэш.
    return _TAG_END_RE.sub(lambda m: f' style="{extra}"{m.group(1)}>', tag_source)


def relax_fixed_widths(html: str, limit: int = EMAIL_WIDTH) -> str:
    """Перевести жёсткие ширины в потолок.

    Трогаем только значения больше ширины письма: таблица на 600 px и уже — это
    осознанная вёрстка, и менять её нельзя. Проценты не трогаем вовсе:
    ``width="100%"`` и так тянется по месту.
    """
    if not html:
        return html

    def fix_table(match: re.Match[str]) -> str:
        tag = match.group(0)
        width = _WIDTH_ATTR_RE.search(tag)
        if not width:
            return tag
        value = int(width.group(1))
        if value <= limit:
            return tag
        without_width = tag[: width.start()] + tag[width.end() :]
        return _with_style(without_width, f"max-width:{value}px;width:100%")

    out = _TABLE_TAG_RE.sub(fix_table, html)

    def fix_style(match: re.Match[str]) -> str:
        quote, css = match.group(1), match.group(2)

        def fix_css(inner: re.Match[str]) -> str:
            value = int(inner.group(3))
            if value <= limit:
                return inner.group(0)
            return f"{inner.group(1)}{inner.group(2)}max-width:{value}px;width:100%"

        fixed = _CSS_WIDTH_RE.sub(fix_css, css)
        return match.group(0) if fixed == css else f" style={quote}{fixed}{quote}"

    out = _STYLE_ATTR_RE.sub(fix_style, out)

    def fix_img(match: re.Match[str]) -> str:
        tag = match.group(0)
        # Атрибут ширины оставляем — на него опирается Outlook, — но добавляем
        # потолок, иначе широкая картинка тянет письмо так же, как таблица.
        if "max-width" in tag.lower():
            return tag
        return _with_style(tag, "max-width:100%;height:auto")

    return _IMG_TAG_RE.sub(fix_img, out)


# Содержимое <head>, без которого письмо плохо себя ведёт у получателя.
#
# Редактор отдаёт пустой <head>, и это заметно на телефоне: без viewport
# почтовый клиент показывает письмо в ширине рабочего стола и мельчит шрифт так,
# что читать нельзя. Стили — страховка на случай вёрстки, которую не удалось
# распознать: клиенты, понимающие <style>, не дадут ей уехать вбок.
_HEAD_EXTRAS = (
    '<meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    "<style>img{max-width:100%;height:auto}table{max-width:100%}"
    "td,th{word-break:break-word}body{margin:0;padding:0;-webkit-text-size-adjust:100%}</style>"
)


def ensure_email_document(html: str) -> str:
    """Привести письмо к виду законченного документа с заполненным ``<head>``.

    Фрагмент оборачивается, документ — дополняется. Повторный вызов ничего не
    портит: по признаку charset видно, что страховка уже на месте.
    """
    if not html:
        return html
    if not is_full_document(html):
        return f"<!DOCTYPE html><html><head>{_HEAD_EXTRAS}</head><body>{html}</body></html>"
    if _CHARSET_RE.search(html):
        return html

    head = _HEAD_RE.search(html)
    if head:
        return html[: head.end()] + _HEAD_EXTRAS + html[head.end() :]

    body = _BODY_OPEN_RE.search(html)
    if body:
        return html[: body.start()] + f"<head>{_HEAD_EXTRAS}</head>" + html[body.start() :]

    opening = _DOCUMENT_RE.search(html)
    if opening:
        return html[: opening.end()] + f"<head>{_HEAD_EXTRAS}</head>" + html[opening.end() :]
    return html


def insert_into_body(html: str, *, before: str = "", after: str = "") -> str:
    """Положить разметку внутрь ``<body>``, а не рядом с документом.

    Раньше шапка и подвал приклеивались до ``<!DOCTYPE>`` и после ``</html>``.
    Часть почтовых клиентов такое отбрасывает, и вместе с подвалом исчезала
    ссылка «Отписаться» — получателю оставалась кнопка «Спам».
    """
    if not before and not after:
        return html
    if not is_full_document(html):
        return f"{before}{html}{after}"

    out = html
    if before:
        body = _BODY_OPEN_RE.search(out)
        out = out[: body.end()] + before + out[body.end() :] if body else before + out
    if after:
        close = _BODY_CLOSE_RE.search(out)
        out = out[: close.start()] + after + out[close.start() :] if close else out + after
    return out
