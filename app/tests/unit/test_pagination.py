"""Логика постраничной навигации — чистая, без БД."""

from __future__ import annotations

import pytest

from cbmail.web.pagination import Page, offset, parse_page


def _page(total: int, page: int, per_page: int = 25) -> Page:
    return Page(items=[], page=page, per_page=per_page, total=total)


def test_pages_rounds_up():
    assert _page(0, 1).pages == 1
    assert _page(1, 1).pages == 1
    assert _page(25, 1).pages == 1
    assert _page(26, 1).pages == 2
    assert _page(50, 1).pages == 2
    assert _page(51, 1).pages == 3


def test_has_prev_next_bounds():
    p = _page(60, 1)
    assert not p.has_prev and p.has_next
    mid = _page(60, 2)
    assert mid.has_prev and mid.has_next
    last = _page(60, 3)
    assert last.has_prev and not last.has_next


def test_indices():
    p = _page(60, 2)
    assert p.start_index == 26
    assert p.end_index == 50
    last = _page(55, 3)
    assert last.start_index == 51
    assert last.end_index == 55  # хвост меньше страницы
    empty = _page(0, 1)
    assert empty.start_index == 0 and empty.end_index == 0


def test_window_clamped():
    assert _page(500, 1).window(radius=2) == [1, 2, 3]
    assert _page(500, 10).window(radius=2) == [8, 9, 10, 11, 12]
    assert _page(500, 20).window(radius=2) == [18, 19, 20]  # 500/25 = 20 страниц


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", 1), ("3", 3), (None, 1), ("0", 1), ("-5", 1), ("abc", 1), ("2.5", 1)],
)
def test_parse_page(raw, expected):
    assert parse_page(raw) == expected


def test_offset():
    assert offset(1, 25) == 0
    assert offset(2, 25) == 25
    assert offset(3, 10) == 20
