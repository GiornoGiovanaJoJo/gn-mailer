"""Регресс: метки времени должны иметь Python-сторонний default.

Столбцы создавались Django (``auto_now_add``/``auto_now``) и в боевой БД НЕ имеют
DB-default. Если у модели только ``server_default``, SQLAlchemy опускает колонку в
INSERT → Postgres валит ``NOT NULL`` (prod-only 500). Python-side ``default``
обязателен. На SQLite ошибка не воспроизводится, поэтому проверяем метаданные.
"""

from __future__ import annotations

import pytest

from cbmail.db.models import (
    Campaign,
    CampaignFile,
    CampaignLog,
    Contact,
    ContactList,
    Message,
    MessageFile,
    MessageTemplate,
    Profile,
    SmtpCheckLog,
)

_CASES = [
    (SmtpCheckLog, "checked_at"),
    (Campaign, "created_at"),
    (Campaign, "updated_at"),
    (CampaignFile, "uploaded_at"),
    (CampaignLog, "created_at"),
    (MessageTemplate, "created_at"),
    (MessageTemplate, "updated_at"),
    (Message, "created_at"),
    (Message, "updated_at"),
    (MessageFile, "uploaded_at"),
    (ContactList, "created_at"),
    (Contact, "created_at"),
    (Profile, "date_joined"),
]


@pytest.mark.parametrize(("model", "column"), _CASES)
def test_timestamp_column_has_python_default(model, column):
    col = model.__table__.c[column]
    assert col.default is not None, (
        f"{model.__name__}.{column}: нужен Python-side default "
        f"(иначе INSERT падает на проде — NOT NULL)"
    )
