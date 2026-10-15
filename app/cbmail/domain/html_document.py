"""Подготовка HTML письма к отправке.

Правило здесь одно, и оно важнее всех удобств: **вёрстку письма не трогаем**.
Письмо свёрстано таблицами с фиксированными ширинами, и это не устаревший
приём, а единственный способ получить одинаковую картинку в десятке почтовых
клиентов. Любая попытка «сделать его отзывчивым» на лету ломает ровно то, что
автор шаблона собирал руками.

Чему здесь научил опыт — тому, что именно ломается:

* ``<meta name="viewport" content="width=device-width">`` переключает мобильный
  клиент из режима «нарисовать как на десктопе и уменьшить» в режим «рисовать
  по ширине телефона». Для шаблона без медиазапросов это катастрофа: таблица на
  600 px начинает ужиматься колонками. Поэтому viewport добавляется ТОЛЬКО
  шаблонам, где медиазапросы есть, то есть автор сам предусмотрел узкий экран.
* ``table{max-width:100%}`` ужимает фиксированную таблицу, у которой ширина
  выставлена осознанно.
* ``td,th{word-break:break-word}`` разрешает рвать слова в любом месте. У
  колонки появляется минимальная ширина в один символ, и меню «о компании ·
  каталог · новости» превращается в три столбика букв по вертикали.
* ``width:100%`` рядом с ``max-width`` растягивает таблицу на всю ширину окна:
  на широком экране текст расползается и наезжает на картинки.

Остаётся то, что раскладку не меняет:

* кодировка — без неё кириллица превращается в кракозябры;
* потолок для картинок: изображение в 2000 px распирает письмо независимо от
  вёрстки, а ``max-width:100%`` всего лишь не даёт ему вылезти за свою ячейку;
* место для шапки, подвала и ссылки отписки ВНУТРИ ``<body>``. Редактор отдаёт
  законченный документ с ``<html>``, а они приклеивались снаружи: часть письма
  оказывалась после ``</html>``. Строгие клиенты такое выбрасывают — и вместе с
  подвалом пропадала ссылка «Отписаться», ради которой всё задумывалось.

Разбор регулярными выражениями, а не парсером: трогаем только теги картинок и
границы документа, и полноценное дерево ради этого тянуть незачем. Неизвестную
разметку функции оставляют как есть.
"""

from __future__ import annotations

import re

# Ширина колонки письма — для вызывающего кода, который считает раскладку.
EMAIL_WIDTH = 600

_DOCUMENT_RE = re.compile(r"<html[\s>]", re.IGNORECASE)
_HEAD_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)
_CHARSET_RE = re.compile(r"<meta[^>]+charset", re.IGNORECASE)
_VIEWPORT_RE = re.compile(r"<meta[^>]+viewport", re.IGNORECASE)

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_STYLE_ATTR_RE = re.compile(r"\sstyle\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
_TAG_END_RE = re.compile(r"\s*(/?)>$")
# Медиазапрос в шаблоне означает, что автор сам предусмотрел узкий экран.
_MEDIA_QUERY_RE = re.compile(r"@media\b", re.IGNORECASE)


def is_full_document(html: str) -> bool:
    """Есть ли в строке полноценный документ, а не фрагмент разметки."""
    return bool(_DOCUMENT_RE.search(html or ""))


def is_responsive(html: str) -> bool:
    """Рассчитан ли шаблон на узкий экран — есть ли в нём медиазапросы.

    От этого зависит единственное: добавлять ли viewport. Шаблону с
    медиазапросами он нужен — без него они не сработают. Шаблону без них он
    противопоказан: мобильный клиент перестанет уменьшать письмо целиком и
    начнёт ломать таблицы по колонкам.
    """
    return bool(_MEDIA_QUERY_RE.search(html or ""))


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


def cap_images(html: str) -> str:
    """Не дать картинке вылезти за свою ячейку.

    Единственное вмешательство в разметку, которое себя оправдывает: картинку в
    письмо вставляют как есть, прямо из файла, и снимок на 2000 px распирает
    письмо независимо от того, как оно свёрстано. Атрибут ширины сохраняем — на
    него опирается Outlook, который ``max-width`` не понимает.
    """
    if not html:
        return html

    def fix_img(match: re.Match[str]) -> str:
        tag = match.group(0)
        if "max-width" in tag.lower():
            return tag
        return _with_style(tag, "max-width:100%;height:auto")

    return _IMG_TAG_RE.sub(fix_img, html)


# Кодировка и сброс полей — то, что не меняет раскладку.
_HEAD_BASE = (
    '<meta charset="utf-8">'
    "<style>img{max-width:100%;height:auto}"
    "body{margin:0;padding:0;-webkit-text-size-adjust:100%}</style>"
)

# Viewport — только для шаблонов с медиазапросами (см. is_responsive).
_HEAD_VIEWPORT = '<meta name="viewport" content="width=device-width,initial-scale=1">'


def _head_extras(html: str) -> str:
    extras = _HEAD_BASE
    if is_responsive(html) and not _VIEWPORT_RE.search(html):
        extras += _HEAD_VIEWPORT
    return extras


def ensure_email_document(html: str) -> str:
    """Привести письмо к виду законченного документа с заполненным ``<head>``.

    Фрагмент оборачивается, документ — дополняется. Повторный вызов ничего не
    портит: по признаку charset видно, что страховка уже на месте.
    """
    if not html:
        return html
    extras = _head_extras(html)
    if not is_full_document(html):
        return f"<!DOCTYPE html><html><head>{extras}</head><body>{html}</body></html>"
    if _CHARSET_RE.search(html):
        return html

    head = _HEAD_RE.search(html)
    if head:
        return html[: head.end()] + extras + html[head.end() :]

    body = _BODY_OPEN_RE.search(html)
    if body:
        return html[: body.start()] + f"<head>{extras}</head>" + html[body.start() :]

    opening = _DOCUMENT_RE.search(html)
    if opening:
        return html[: opening.end()] + f"<head>{extras}</head>" + html[opening.end() :]
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
