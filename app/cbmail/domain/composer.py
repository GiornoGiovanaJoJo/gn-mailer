"""Сборка тела письма: шапка, подвал, подстановки, текстовая версия.

Чистые функции без ввода-вывода — вся логика подстановок покрыта тестами.

Главное исправление: текстовая версия письма больше не получается прогоном
HTML через ``strip_tags``. Тот подход оставлял в тексте содержимое ``<style>``
и ``<script>``, склеивал слова из соседних ячеек таблицы и терял ссылки —
получатели с текстовыми клиентами (и превью в списке писем у большинства
почтовых сервисов) видели кашу из CSS вперемешку с обрывками фраз.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser

# Плейсхолдер, который пользователь ставит в тело письма руками — обычно внутри
# ссылки отписки вида https://site/unsubscribe/{{email}}/
EMAIL_PLACEHOLDER = "{{email}}"
UNSUBSCRIBE_PLACEHOLDER = "{{unsubscribe_url}}"


@dataclass(frozen=True, slots=True)
class ComposedMessage:
    subject: str
    text_body: str
    html_body: str | None

    def personalize(self, email: str, unsubscribe_url: str | None = None) -> ComposedMessage:
        """Подставить адрес получателя в заранее собранное тело.

        Выполняется для каждого получателя отдельно, но разбор и сборка тела —
        один раз на кампанию: раньше HTML-шаблон рендерился заново на каждое
        письмо, что на рассылке в 10 000 адресов означало 10 000 рендеров.
        """
        replacements = {EMAIL_PLACEHOLDER: email}
        if unsubscribe_url is not None:
            replacements[UNSUBSCRIBE_PLACEHOLDER] = unsubscribe_url

        return ComposedMessage(
            subject=_apply(self.subject, replacements),
            text_body=_apply(self.text_body, replacements),
            html_body=_apply(self.html_body, replacements) if self.html_body else None,
        )


def _apply(text: str, replacements: dict[str, str]) -> str:
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    return text


def compose(
    *,
    subject: str,
    body: str,
    header: str | None = None,
    footer: str | None = None,
    body_is_html: bool | None = None,
    add_unsubscribe: bool = False,
) -> ComposedMessage:
    """Собрать письмо из тела, шапки и подвала.

    ``body_is_html`` по умолчанию определяется автоматически: тело, пришедшее
    из визуального редактора, содержит разметку, а набранное руками — нет.
    Раньше HTML-версия строилась всегда, даже для простого текста, из-за чего
    переносы строк в набранном вручную письме схлопывались в один абзац.

    ``add_unsubscribe`` (только для массовых рассылок) добавляет ссылку отписки,
    если пользователь не вставил ``{{unsubscribe_url}}`` сам. Ключевой момент:
    даже для письма из простого текста строится HTML-версия, где «Отписаться» —
    это КЛИКАБЕЛЬНОЕ СЛОВО-ссылка, а не голый URL. Текстовая версия (для клиентов
    без HTML) сохраняет адрес ссылки рядом. Плейсхолдер подставляется адресно при
    отправке каждому получателю.
    """
    body = body or ""
    if body_is_html is None:
        body_is_html = looks_like_html(body)

    has_placeholder = UNSUBSCRIBE_PLACEHOLDER in body
    want_footer = add_unsubscribe and not has_placeholder

    if body_is_html:
        html_body = _wrap_html(body, header=header, footer=footer)
        if want_footer:
            html_body += _html_unsub_footer()
        text_body = html_to_text(html_body)
        return ComposedMessage(subject=subject, text_body=text_body, html_body=html_body)

    text_body = _join_parts(header, body, footer)
    if want_footer:
        # HTML-версия из простого текста нужна ровно ради кликабельной «Отписаться».
        html_body = _plain_to_html(text_body) + _html_unsub_footer()
        text_body = text_body + _text_unsub_footer()
        return ComposedMessage(subject=subject, text_body=text_body, html_body=html_body)

    return ComposedMessage(subject=subject, text_body=text_body, html_body=None)


def _join_parts(header: str | None, body: str, footer: str | None) -> str:
    parts = [part.strip() for part in (header, body, footer) if part and part.strip()]
    return "\n\n".join(parts)


def _wrap_html(body: str, *, header: str | None, footer: str | None) -> str:
    """Добавить шапку и подвал к HTML-телу.

    Шапка и подвал задаются в настройках профиля обычным текстом, поэтому
    экранируются — иначе символ ``<`` в подписи ломал вёрстку письма, а
    вставленный туда тег мог изменить его содержимое.
    """
    chunks: list[str] = []
    if header and header.strip():
        chunks.append(f"<div>{_escape_to_html(header)}</div>")
    chunks.append(body)
    if footer and footer.strip():
        chunks.append(f"<div>{_escape_to_html(footer)}</div>")
    return "\n".join(chunks)


def _escape_to_html(text: str) -> str:
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return escaped.replace("\n", "<br>")


_HTML_MARKER_RE = re.compile(
    r"<\s*(?:!doctype|html|body|div|p|table|tr|td|br|span|a|img|h[1-6]|ul|ol|li|strong|em)\b",
    re.IGNORECASE,
)


def looks_like_html(text: str | None) -> bool:
    if not text:
        return False
    return bool(_HTML_MARKER_RE.search(text))


# --- Преобразование HTML в читаемый текст ------------------------------------

# Теги, чьё содержимое в текстовую версию попадать не должно вообще.
_INVISIBLE_TAGS = frozenset({"style", "script", "head", "title", "meta", "link"})

# Теги, после которых нужен перенос строки.
_LINE_BREAK_TAGS = frozenset({"br", "tr", "li", "div", "p", "h1", "h2", "h3", "h4", "h5", "h6"})

# Теги, вокруг которых нужна пустая строка (границы абзацев).
_BLOCK_TAGS = frozenset({"p", "div", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"})

# Ячейки таблицы: между ними нужен разделитель, иначе соседние ячейки
# склеиваются в одно слово. Вёрстка писем почти всегда табличная, поэтому
# случай не краевой, а основной.
_CELL_TAGS = frozenset({"td", "th"})


class _TextExtractor(HTMLParser):
    """Извлекает читаемый текст, сохраняя структуру абзацев и адреса ссылок."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._suppress_depth = 0
        self._link_target: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _INVISIBLE_TAGS:
            self._suppress_depth += 1
            return
        if self._suppress_depth:
            return

        if tag == "a":
            href = dict(attrs).get("href")
            # Якоря и mailto: в тексте бесполезны — дублировать их не нужно.
            if href and not href.startswith(("#", "javascript:")):
                self._link_target = href
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self._parts.append(f"[{alt}]")
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n\n")
        elif tag in _LINE_BREAK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _INVISIBLE_TAGS:
            self._suppress_depth = max(0, self._suppress_depth - 1)
            return
        if self._suppress_depth:
            return

        if tag == "a" and self._link_target:
            # Ссылку выводим рядом с текстом: иначе в текстовой версии
            # предложение «нажмите здесь» теряет всякий смысл.
            self._parts.append(f" ({self._link_target})")
            self._link_target = None
        elif tag in _CELL_TAGS:
            self._parts.append("\t")
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n\n")
        elif tag in _LINE_BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._suppress_depth:
            return
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    """Преобразовать HTML письма в читаемую текстовую версию."""
    if not html:
        return ""

    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Разметка из внешних редакторов бывает некорректной. Текстовая версия
        # важна, но не настолько, чтобы из-за неё не ушло письмо целиком.
        return _fallback_strip_tags(html)

    text = unescape(parser.get_text())
    # Схлопываем пробелы внутри строк, но сохраняем переносы.
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


_TAG_RE = re.compile(r"<[^>]+>")
_INVISIBLE_BLOCK_RE = re.compile(
    r"<(style|script|head)\b.*?</\1>", re.IGNORECASE | re.DOTALL
)


def _fallback_strip_tags(html: str) -> str:
    without_invisible = _INVISIBLE_BLOCK_RE.sub(" ", html)
    text = unescape(_TAG_RE.sub(" ", without_invisible))
    return _BLANK_LINES_RE.sub("\n\n", _WHITESPACE_RE.sub(" ", text)).strip()


def build_unsubscribe_url(base_url: str, email: str, scope: str = "") -> str:
    """Ссылка отписки для конкретного адреса и отправителя.

    Email передаётся через query-параметр ``e`` (не в пути), потому что символ
    ``@`` в URL-пути ненадёжен: nginx/прокси трактуют всё после ``@`` как хост
    и обрезают путь, что приводит к 400 при любом кодировании.

    ``scope`` — домен отправителя. Он попадает в параметр ``d`` и делает отписку
    адресной: человек, отписавшийся от писем одного домена, продолжает получать
    письма от другого. Пустой ``scope`` (старые письма) означает глобальную
    отписку — обратная совместимость сохранена.

    Формат: {base_url}/unsubscribe?e=user%40domain.com&d=sender-domain.ru
    """
    from urllib.parse import quote

    url = f"{base_url.rstrip('/')}/unsubscribe?e={quote(email, safe='')}"
    if scope:
        url += f"&d={quote(scope, safe='')}"
    return url


_UNSUB_TEXT = (
    "Вы получили это письмо, потому что ваш адрес есть в списке рассылки."
)


def _html_unsub_footer() -> str:
    """HTML-подвал: «Отписаться» — кликабельное слово-ссылка."""
    return (
        '\n<div style="margin-top:24px;padding-top:12px;border-top:1px solid #e5e5e5;'
        'font-size:12px;color:#888888;text-align:center">'
        f'{_UNSUB_TEXT} '
        f'<a href="{UNSUBSCRIBE_PLACEHOLDER}" style="color:#888888">Отписаться</a></div>'
    )


def _text_unsub_footer() -> str:
    """Текстовый подвал: для клиентов без HTML показываем адрес ссылки рядом."""
    return f"\n\n—\n{_UNSUB_TEXT}\nОтписаться: {UNSUBSCRIBE_PLACEHOLDER}"


def _plain_to_html(text: str) -> str:
    """Простой текст → минимальный HTML: экранирование + переносы строк.

    Нужен, чтобы у письма из простого текста была HTML-версия, в которой ссылка
    отписки кликабельна. Без неё «Отписаться» пришлось бы показывать голым URL.
    """
    return f'<div style="white-space:pre-wrap">{_escape_to_html(text)}</div>'


def combine_body(template_body: str | None, extra_text: str | None) -> str:
    """Объединить готовый шаблон и собственный текст пользователя.

    Поддерживаются все сочетания, как и просил пользователь:

    * только шаблон (текст пуст) → возвращается шаблон;
    * только текст (шаблон не выбран) → возвращается текст;
    * и то и другое → СВЕРХУ идёт личный текст пользователя, НИЖЕ — оформление
      шаблона. Так письмо начинается с обращения, а под ним — готовый макет.

    Если в игре есть HTML (обычно шаблон из конструктора), результат — HTML:
    простой текст пользователя аккуратно оборачивается, чтобы переносы строк не
    схлопнулись и вёрстка шаблона не поехала.
    """
    template_body = template_body or ""
    extra_text = (extra_text or "").strip()

    if not template_body:
        return extra_text
    if not extra_text:
        return template_body

    if looks_like_html(template_body) or looks_like_html(extra_text):
        extra_html = extra_text if looks_like_html(extra_text) else _plain_to_html(extra_text)
        return f"{extra_html}\n{template_body}"
    return f"{extra_text}\n\n{template_body}"
