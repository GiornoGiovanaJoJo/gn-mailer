"""Небольшой помощник постраничной навигации для списков.

Держим отдельно, чтобы списки (письма, а в будущем и кампании/шаблоны) считали
страницы одинаково, а не копировали арифметику ``offset/limit`` по месту.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Page:
    items: list
    page: int
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.per_page - 1) // self.per_page)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def start_index(self) -> int:
        return 0 if self.total == 0 else (self.page - 1) * self.per_page + 1

    @property
    def end_index(self) -> int:
        return min(self.page * self.per_page, self.total)

    def window(self, radius: int = 2) -> list[int]:
        lo = max(1, self.page - radius)
        hi = min(self.pages, self.page + radius)
        return list(range(lo, hi + 1))


def parse_page(raw: str | None, *, default: int = 1) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return max(1, value)


def offset(page: int, per_page: int) -> int:
    return (page - 1) * per_page
