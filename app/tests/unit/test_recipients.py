"""Разбор файла со списком получателей.

Проверяем ровно то, на чём этот код ломается в жизни: чужие кодировки,
адрес не в первой колонке, старый .xls и повторы в выгрузке.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from cbmail.domain.recipients import (
    MAX_FILE_BYTES,
    ImportResult,
    RecipientFileError,
    extract_emails,
)


def _xlsx(rows: list[list[object]]) -> bytes:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_text_list_with_separators() -> None:
    data = b"ivan@example.ru, petr@example.ru; anna@example.ru\nolga@example.ru"
    result = extract_emails("list.txt", data)
    assert result.emails == [
        "ivan@example.ru",
        "petr@example.ru",
        "anna@example.ru",
        "olga@example.ru",
    ]


def test_cp1251_csv_is_read() -> None:
    # Выгрузки из старых российских систем приходят в cp1251, и utf-8 на них
    # падает — файл должен читаться, а не отвергаться.
    data = "Имя;Почта\nИван;ivan@example.ru\nПётр;petr@example.ru".encode("cp1251")
    result = extract_emails("списки.csv", data)
    assert result.emails == ["ivan@example.ru", "petr@example.ru"]


def test_duplicates_and_garbage_are_counted() -> None:
    # «ivan..petr@example.ru» на вид адрес, но две точки подряд в локальной части
    # запрещены. Такие строки считаются отдельно от повторов — в интерфейсе
    # нужно честно сказать, сколько строк не принято и почему.
    data = "ivan@example.ru\nIVAN@example.ru\nсовсем не адрес\nivan..petr@example.ru\n".encode()
    result = extract_emails("list.txt", data)
    assert result.emails == ["ivan@example.ru"]
    assert result.skipped_duplicates == 1
    assert result.skipped_invalid == 1
    # Строка без «собаки» на адрес не похожа вовсе и в счётчики не попадает.
    assert result.total_found == 3


def test_order_is_preserved() -> None:
    # Порядок задаёт очерёдность рассылки, и человек ждёт порядок своего файла.
    data = b"zina@example.ru\nanna@example.ru\nboris@example.ru"
    assert extract_emails("list.txt", data).emails == [
        "zina@example.ru",
        "anna@example.ru",
        "boris@example.ru",
    ]


def test_xlsx_any_column_and_sheet() -> None:
    data = _xlsx([
        ["ФИО", "Телефон", "Почта"],
        ["Иванов", "+7 900 000-00-00", "ivan@example.ru"],
        ["Петров", "", "petr@example.ru"],
    ])
    result = extract_emails("выгрузка.xlsx", data)
    assert result.emails == ["ivan@example.ru", "petr@example.ru"]


def test_xlsx_numbers_do_not_break_parsing() -> None:
    data = _xlsx([[1, 2.5, None, "anna@example.ru"]])
    assert extract_emails("x.xlsx", data).emails == ["anna@example.ru"]


def test_old_xls_falls_back_to_binary_scan() -> None:
    # Сигнатура OLE2 и адреса внутри: так выглядит нестандартный .xls, который
    # библиотека прочитать не смогла.
    blob = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00\x01" + b"ivan@example.ru" + b"\x00\x00" + b"anna@example.ru"
    result = extract_emails("старый.xls", blob)
    assert result.emails == ["ivan@example.ru", "anna@example.ru"]


def test_binary_scan_does_not_glue_junk_to_address() -> None:
    # Нестрогое правило приклеивало служебные байты к имени пользователя и
    # выдавало адрес вида «ÐÏàivan@example.ru».
    blob = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1\xd0\xcf\xe0ivan@example.ru\x00"
    assert extract_emails("x.xls", blob).emails == ["ivan@example.ru"]


def test_unknown_extension_rejected() -> None:
    with pytest.raises(RecipientFileError, match="Принимаются файлы"):
        extract_emails("база.pdf", b"ivan@example.ru")


def test_empty_file_rejected() -> None:
    with pytest.raises(RecipientFileError, match="пуст"):
        extract_emails("list.txt", b"")


def test_file_without_addresses_rejected() -> None:
    with pytest.raises(RecipientFileError, match="ни одного адреса"):
        extract_emails("list.txt", "здесь только текст".encode())


def test_too_large_file_rejected() -> None:
    with pytest.raises(RecipientFileError, match="МБ"):
        extract_emails("list.txt", b"x" * (MAX_FILE_BYTES + 1))


def test_result_is_immutable_snapshot() -> None:
    # Результат разбора — снимок: случайная правка на месте потом расходится с
    # тем, что показали человеку в отчёте «принято столько-то».
    result = extract_emails("list.txt", b"ivan@example.ru")
    assert isinstance(result, ImportResult)
    with pytest.raises(AttributeError):
        result.emails = []  # type: ignore[misc]
