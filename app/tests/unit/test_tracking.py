"""Трекинг: подписи, пиксель и переписывание ссылок.

Главное, что здесь проверяется, — невозможность накрутить счётчик и увести
получателя на чужой адрес: ссылка перехода подписана вместе с адресом
назначения, иначе наш домен превращается в открытый редирект для фишинга.
"""

from __future__ import annotations

from cbmail.domain.tracking import (
    click_signature,
    click_url,
    inject,
    make_token,
    parse_token,
    verify_click,
)

SECRET = "тестовый-ключ"
BASE = "https://mail.example.ru"


def test_token_roundtrip() -> None:
    token = make_token(SECRET, 42, "ivan@example.ru")
    assert parse_token(SECRET, token) == (42, "ivan@example.ru")


def test_token_with_broken_signature_rejected() -> None:
    token = make_token(SECRET, 42, "ivan@example.ru")
    payload, _, signature = token.partition(".")
    assert parse_token(SECRET, f"{payload}.{signature[:-2]}xx") is None


def test_token_signed_by_another_key_rejected() -> None:
    token = make_token("чужой-ключ", 42, "ivan@example.ru")
    assert parse_token(SECRET, token) is None


def test_garbage_token_rejected() -> None:
    for junk in ("", "abc", "abc.def", "...", "%%%.%%%"):
        assert parse_token(SECRET, junk) is None


def test_click_signature_binds_target_url() -> None:
    token = make_token(SECRET, 1, "ivan@example.ru")
    signature = click_signature(SECRET, token, "https://example.ru/акция")
    assert verify_click(SECRET, token, "https://example.ru/акция", signature)
    # Подменили адрес — подпись не сходится, редиректа не будет.
    assert not verify_click(SECRET, token, "https://фишинг.рф", signature)


def test_pixel_added_before_body_close() -> None:
    html = "<html><body><p>Привет</p></body></html>"
    out = inject(html, base_url=BASE, secret=SECRET, campaign_id=7, email="ivan@example.ru")
    assert out is not None
    assert out.index("/t/o/") < out.index("</body>")
    assert out.count("<img") == 1


def test_pixel_appended_when_no_body_tag() -> None:
    out = inject("<p>Привет</p>", base_url=BASE, secret=SECRET, campaign_id=7, email="a@b.ru")
    assert out is not None and out.endswith("/>")
    assert "/t/o/" in out


def test_links_rewritten_to_click_tracker() -> None:
    html = '<a href="https://example.ru/sale">Смотреть</a>'
    out = inject(html, base_url=BASE, secret=SECRET, campaign_id=7, email="ivan@example.ru")
    assert out is not None
    assert f'href="{BASE}/t/c/' in out
    assert "u=https%3A%2F%2Fexample.ru%2Fsale" in out


def test_own_links_are_not_wrapped() -> None:
    # Отписку и сам трекинг заворачивать нельзя: переход считался бы кликом по
    # рассылке, а отписка вела бы по кругу.
    html = (
        f'<a href="{BASE}/unsubscribe/ivan@example.ru">Отписаться</a>'
        f'<a href="{BASE}/t/c/xxx">Служебная</a>'
    )
    out = inject(html, base_url=BASE, secret=SECRET, campaign_id=7, email="ivan@example.ru")
    assert out is not None
    assert out.count("/t/c/") == 1  # только исходная служебная ссылка


def test_mailto_and_tel_untouched() -> None:
    html = '<a href="mailto:sales@example.ru">Написать</a><a href="tel:+79000000000">Позвонить</a>'
    out = inject(html, base_url=BASE, secret=SECRET, campaign_id=7, email="a@b.ru")
    assert out is not None
    assert "mailto:sales@example.ru" in out
    assert "tel:+79000000000" in out


def test_unsubscribe_placeholder_replaced() -> None:
    html = '<a href="{{unsubscribe_url}}">Отписаться</a>'
    out = inject(
        html,
        base_url=BASE,
        secret=SECRET,
        campaign_id=7,
        email="a@b.ru",
        unsubscribe_url=f"{BASE}/unsubscribe/a@b.ru",
    )
    assert out is not None
    assert "{{unsubscribe_url}}" not in out
    assert f"{BASE}/unsubscribe/a@b.ru" in out


def test_without_base_url_nothing_is_injected() -> None:
    # Ссылка на несуществующий хост в письме хуже отсутствующей статистики.
    html = '<a href="https://example.ru">Тут</a>'
    out = inject(html, base_url="", secret=SECRET, campaign_id=7, email="a@b.ru")
    assert out == html


def test_tracking_can_be_switched_off() -> None:
    html = '<body><a href="https://example.ru">Тут</a></body>'
    out = inject(
        html,
        base_url=BASE,
        secret=SECRET,
        campaign_id=7,
        email="a@b.ru",
        track_opens=False,
        track_clicks=False,
    )
    assert out == html


def test_empty_body_is_passed_through() -> None:
    assert inject(None, base_url=BASE, secret=SECRET, campaign_id=1, email="a@b.ru") is None
    assert inject("", base_url=BASE, secret=SECRET, campaign_id=1, email="a@b.ru") == ""


def test_click_url_is_stable_for_same_inputs() -> None:
    token = make_token(SECRET, 3, "ivan@example.ru")
    first = click_url(BASE, token, SECRET, "https://example.ru/a")
    second = click_url(BASE, token, SECRET, "https://example.ru/a")
    assert first == second


def test_non_ascii_signature_is_rejected_not_crashed() -> None:
    """Подпись из адреса письма может содержать что угодно, включая кириллицу.

    hmac.compare_digest на строках с не-ASCII бросает TypeError: ссылка вида
    «?s=подделка» роняла обработчик пятисотой вместо честного отказа.
    """
    token = make_token(SECRET, 1, "ivan@example.ru")
    assert not verify_click(SECRET, token, "https://example.ru", "подделка")
    assert not verify_click(SECRET, token, "https://example.ru", "🙂")


def test_non_ascii_token_signature_is_rejected() -> None:
    assert parse_token(SECRET, "payload.подпись") is None
    assert parse_token(SECRET, "пайлоад.подпись") is None


def test_non_ascii_target_url_still_verifies() -> None:
    # Кириллица в адресе назначения — обычное дело для рунета, и подпись
    # обязана сходиться.
    token = make_token(SECRET, 1, "ivan@example.ru")
    target = "https://пример.рф/акция"
    assert verify_click(SECRET, token, target, click_signature(SECRET, token, target))
