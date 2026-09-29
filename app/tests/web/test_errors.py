"""Ответ сервиса на неожиданную ошибку.

Проверяем не текст ради текста, а две вещи, которые решают разбор инцидента:
код обращения виден человеку и попадает в лог, а «база отстала от кода»
называется прямо, вместе с командой, которой чинится.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cbmail.web.errors import install_error_handlers


class _Drift(Exception):
    """Как выглядит отсутствие колонки в ответе драйвера Postgres."""


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    def boom():
        raise RuntimeError("что-то сломалось")

    @app.get("/drift")
    def drift():
        raise _Drift('column "opened_at" of relation "mail_massmaillog" does not exist')

    return app


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False — иначе клиент пробрасывает исключение
    # наружу и обработчик не успевает отработать.
    return TestClient(_app(), raise_server_exceptions=False)


def test_html_page_carries_incident_code(client: TestClient) -> None:
    resp = client.get("/boom", headers={"accept": "text/html"})
    assert resp.status_code == 500
    assert "Что-то пошло не так" in resp.text
    assert re.search(r"<code>[0-9a-f]{8}</code>", resp.text)


def test_json_client_gets_incident_code(client: TestClient) -> None:
    resp = client.get("/boom", headers={"accept": "application/json"})
    assert resp.status_code == 500
    body = resp.json()
    assert re.fullmatch(r"[0-9a-f]{8}", body["incident"])


def test_incident_codes_differ(client: TestClient) -> None:
    # Код бесполезен, если у двух разных сбоев он одинаковый.
    first = client.get("/boom", headers={"accept": "application/json"}).json()["incident"]
    second = client.get("/boom", headers={"accept": "application/json"}).json()["incident"]
    assert first != second


def test_schema_drift_is_named_and_actionable(client: TestClient) -> None:
    resp = client.get("/drift", headers={"accept": "application/json"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "SCHEMA_OUT_OF_DATE"
    assert "init_session" in body["error"]


def test_schema_drift_page_for_browser(client: TestClient) -> None:
    resp = client.get("/drift", headers={"accept": "text/html"})
    assert resp.status_code == 503
    assert "База данных не готова" in resp.text
    assert "init_session" in resp.text


def test_incident_is_logged_with_the_same_code(client: TestClient, caplog) -> None:
    # Код на странице без кода в логе бесполезен: сопоставить нечем.
    with caplog.at_level("ERROR"):
        body = client.get("/boom", headers={"accept": "application/json"}).json()
    assert body["incident"] in caplog.text
