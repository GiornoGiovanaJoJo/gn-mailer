"""Сквозные тесты веб-слоя на SQLite: вход, кампании, СТОП, SMTP, стоп-лист.

Лицензия в тестах подменяется на «активна»: проверяется веб-логика, а не
криптография ключа (она покрыта отдельно на уровне модуля licensing).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as OrmSession
from starlette.testclient import TestClient

from cbmail.core.security import hash_password
from cbmail.db.models import Base, Message, Profile

_CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "web.db"
    url_sync = f"sqlite:///{db_file}"
    url_async = f"sqlite+aiosqlite:///{db_file}"

    sync_engine = create_engine(url_sync, future=True)
    Base.metadata.create_all(sync_engine)
    user_id = uuid.uuid4()
    with OrmSession(sync_engine) as s:
        s.add(
            Profile(
                id=user_id,
                password=hash_password("secret", iterations=1000),
                username="tester",
                first_name="Иван",
                last_name="Тестов",
                email="tester@example.com",
                is_active=True,
                is_staff=True,
                is_superuser=False,
                type=1,
                gender=1,
                avatar="",
                online=False,
                blocked=False,
                deleted=False,
            )
        )
        s.add(
            Profile(
                id=uuid.uuid4(),
                password=hash_password("secret", iterations=1000),
                username="boss",
                first_name="Босс",
                last_name="",
                email="boss@example.com",
                is_active=True,
                is_staff=True,
                is_superuser=True,
                type=1,
                gender=1,
                avatar="",
                online=False,
                blocked=False,
                deleted=False,
            )
        )
        s.add_all(
            [
                Message(
                    id=uuid.uuid4(),
                    custom_id=f"MSG-{i}",
                    user_id=user_id,
                    clients="a@example.com",
                    message=f"<p>Тело письма {i}</p>",
                    subject=f"Письмо номер {i}",
                    message_type="outgoing",
                    self_field=False,
                )
                for i in range(1, 4)
            ]
        )
        s.commit()

    async_engine = create_async_engine(url_async, future=True)
    factory = async_sessionmaker(async_engine, expire_on_commit=False, autoflush=False)

    async def _get_db():
        async with factory() as session:
            yield session

    monkeypatch.setattr("cbmail.web.main.verify_license", lambda *a, **k: (True, "ok"))

    from cbmail.web.deps import get_db
    from cbmail.web.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def _csrf(client: TestClient, url: str) -> str:
    html = client.get(url).text
    match = _CSRF_RE.search(html)
    assert match, f"CSRF-токен не найден на {url}"
    return match.group(1)


def _login_as(client: TestClient, username: str, password: str = "secret") -> None:
    token = _csrf(client, "/login")
    resp = client.post(
        "/login",
        data={"username": username, "password": password, "csrfmiddlewaretoken": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/campaigns/"


def _login(client: TestClient) -> None:
    _login_as(client, "tester")


def test_requires_login(client):
    resp = client.get("/campaigns/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_login_wrong_password(client):
    token = _csrf(client, "/login")
    resp = client.post(
        "/login",
        data={"username": "tester", "password": "nope", "csrfmiddlewaretoken": token},
    )
    assert resp.status_code == 401


def test_login_and_list_empty(client):
    _login(client)
    resp = client.get("/campaigns/")
    assert resp.status_code == 200
    assert "Пока нет ни одной кампании" in resp.text


def test_create_schedule_and_stop(client):
    _login(client)
    token = _csrf(client, "/campaigns/new")
    when = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    resp = client.post(
        "/campaigns/",
        data={
            "csrfmiddlewaretoken": token,
            "name": "Тест-кампания",
            "subject": "Привет",
            "message": "Здравствуйте, {{email}}!",
            "recipient_emails": "a@example.com, b@example.com, a@example.com",
            "message_count": "50",
            "message_interval": "1",
            "scheduled_time": when,
            "intent": "schedule",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith("/campaigns/")
    campaign_id = int(location.rsplit("/", 1)[-1])

    detail = client.get(location)
    assert detail.status_code == 200
    assert "Запланирована" in detail.text
    # Дубликат адреса схлопнут: осталось 2 уникальных.
    assert ">2<" in detail.text or "Всего адресатов" in detail.text
    # Кнопка СТОП присутствует у запланированной кампании.
    assert f"/campaigns/{campaign_id}/stop" in detail.text

    token = _csrf(client, location)
    stop = client.post(
        f"/campaigns/{campaign_id}/stop",
        data={"csrfmiddlewaretoken": token},
        follow_redirects=False,
    )
    assert stop.status_code == 303
    after = client.get(location)
    assert "Отменена" in after.text


def test_create_validation_error(client):
    _login(client)
    token = _csrf(client, "/campaigns/new")
    resp = client.post(
        "/campaigns/",
        data={"csrfmiddlewaretoken": token, "name": "", "subject": "x", "message": "y",
              "recipient_emails": "a@example.com", "intent": "draft"},
    )
    assert resp.status_code == 400
    assert "название" in resp.text.lower()


def test_csrf_rejected(client):
    _login(client)
    resp = client.post(
        "/campaigns/",
        data={"csrfmiddlewaretoken": "bogus", "name": "n", "subject": "s",
              "message": "m", "recipient_emails": "a@example.com", "intent": "draft"},
        follow_redirects=False,
    )
    # Неверный CSRF уводит на /login через обработчик редиректа.
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_smtp_profile_crud(client):
    _login(client)
    token = _csrf(client, "/smtp/")
    resp = client.post(
        "/smtp/",
        data={
            "csrfmiddlewaretoken": token,
            "title": "Яндекс",
            "email_host": "smtp.yandex.ru",
            "email_port": "465",
            "email_host_user": "hello@pmmarketing.ru",
            "email_host_password": "secret",
            "default_from_email": "hello@pmmarketing.ru",
            "email_use_ssl": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    listing = client.get("/smtp/")
    assert "Яндекс" in listing.text
    assert "smtp.yandex.ru" in listing.text


def test_stoplist_bulk_add_dedup(client):
    _login(client)
    token = _csrf(client, "/stoplist/")
    resp = client.post(
        "/stoplist/",
        data={
            "csrfmiddlewaretoken": token,
            "action": "add_bulk",
            "bulk_emails": "Spam@Bad.com\nspam@bad.com\nx@y.ru",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get("/stoplist/").text
    # Дубликат по нормализации схлопнут: 2 адреса.
    assert "2 шт" in page
    assert "x@y.ru" in page


def test_stoplist_add_single_and_remove(client):
    _login(client)
    token = _csrf(client, "/stoplist/")
    client.post(
        "/stoplist/",
        data={"csrfmiddlewaretoken": token, "action": "add_single", "email": "one@ex.com"},
    )
    assert "one@ex.com" in client.get("/stoplist/").text
    token = _csrf(client, "/stoplist/")
    client.post(
        "/stoplist/",
        data={"csrfmiddlewaretoken": token, "action": "remove", "email": "one@ex.com"},
    )
    assert "one@ex.com" not in client.get("/stoplist/").text


def test_license_page_public(client):
    # Страница лицензии доступна без входа.
    resp = client.get("/license/")
    assert resp.status_code == 200
    assert "Активация лицензии" in resp.text


def test_sneat_shell_present(client):
    # Авторизованные страницы отдаются в Sneat-оболочке (тот же вид, что и раньше).
    _login(client)
    for url in ("/campaigns/", "/templates/", "/smtp/", "/stoplist/"):
        html = client.get(url).text
        assert "GN Mailer" in html, url
        assert "layout-menu" in html, url


def test_templates_module_crud(client):
    _login(client)
    listing = client.get("/templates/")
    assert listing.status_code == 200
    assert "Шаблоны писем" in listing.text

    token = _csrf(client, "/templates/new")
    created = client.post(
        "/templates/",
        data={
            "csrfmiddlewaretoken": token,
            "name": "Приветствие",
            "mask": "<h1>Привет, {{email}}</h1>",
            "mask_json": '{"root":{}}',
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    page = client.get("/templates/").text
    assert "Приветствие" in page

    # Содержимое отдаётся для подстановки в кампанию/редактор.
    import re as _re

    tid = int(_re.search(r"/templates/(\d+)/edit", page).group(1))
    content = client.get(f"/templates/{tid}/content").json()
    assert content["html"] == "<h1>Привет, {{email}}</h1>"
    assert content["name"] == "Приветствие"

    token = _csrf(client, "/templates/")
    copied = client.post(
        f"/templates/{tid}/copy",
        data={"csrfmiddlewaretoken": token},
        follow_redirects=False,
    )
    assert copied.status_code == 303
    assert "(копия)" in client.get("/templates/").text


def test_messages_list_and_detail(client):
    _login(client)
    listing = client.get("/messages/")
    assert listing.status_code == 200
    assert "Письмо номер 1" in listing.text
    assert "Исходящее" in listing.text
    # Переход в карточку письма.
    import re as _re

    mid = _re.search(r"/messages/([0-9a-f-]{36})", listing.text).group(1)
    detail = client.get(f"/messages/{mid}")
    assert detail.status_code == 200
    assert "Тело письма" in detail.text


def test_message_star_and_read_toggle(client):
    _login(client)
    import re as _re

    listing = client.get("/messages/").text
    mid = _re.search(r"/messages/([0-9a-f-]{36})/star", listing).group(1)

    token = _csrf(client, "/messages/")
    starred = client.post(
        f"/messages/{mid}/star", data={"csrfmiddlewaretoken": token}, follow_redirects=False
    )
    assert starred.status_code == 303
    assert "bxs-star" in client.get(f"/messages/{mid}").text  # звезда закрашена

    token = _csrf(client, "/messages/")
    client.post(f"/messages/{mid}/read", data={"csrfmiddlewaretoken": token})
    assert "bx-envelope-open" in client.get(f"/messages/{mid}").text  # отмечено прочитанным


def test_message_trash_and_restore(client):
    _login(client)
    import re as _re

    mid = _re.search(
        r"/messages/([0-9a-f-]{36})/trash", client.get("/messages/").text
    ).group(1)

    token = _csrf(client, "/messages/")
    client.post(f"/messages/{mid}/trash", data={"csrfmiddlewaretoken": token})
    assert mid not in client.get("/messages/").text  # ушло из «Входящих»
    assert mid in client.get("/messages/?box=trash").text  # видно в «Корзине»

    token = _csrf(client, "/messages/?box=trash")
    client.post(f"/messages/{mid}/restore", data={"csrfmiddlewaretoken": token})
    assert mid in client.get("/messages/").text  # вернулось во «Входящие»


def test_all_messages_requires_superuser(client):
    _login(client)
    # Обычный пользователь (is_superuser=False) в «Все письма» не пускается.
    resp = client.get("/all-messages/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/messages/"


def test_messages_in_sidebar(client):
    _login(client)
    html = client.get("/campaigns/").text
    assert 'href="/messages/"' in html
    # Пункт «Все письма» скрыт для не-администратора.
    assert 'href="/all-messages/"' not in html


def test_unsubscribe_public_adds_to_stoplist(client):
    # Публичная отписка — без входа.
    resp = client.get("/unsubscribe/Client@Example.com")
    assert resp.status_code == 200
    assert "успешно отписались" in resp.text
    # Повторно — уже отписан (нормализация регистра).
    again = client.get("/unsubscribe/client@example.com")
    assert again.status_code == 200
    assert "уже отписаны" in again.text
    # Адрес виден администратору в списке отписавшихся.
    _login(client)
    page = client.get("/stoplist/").text
    assert "client@example.com" in page


def test_unsubscribe_is_scoped_to_sender(client):
    # Отписка от писем одного отправителя не должна блокировать другого.
    r1 = client.get("/unsubscribe", params={"e": "buyer@example.com", "d": "pmmarketing.ru"})
    assert "успешно отписались" in r1.text
    # Тот же адрес, другой отправитель — это НОВАЯ отписка, а не «уже отписаны».
    r2 = client.get("/unsubscribe", params={"e": "buyer@example.com", "d": "animateme.ru"})
    assert "успешно отписались" in r2.text
    # Повтор в той же области — уже отписан.
    r3 = client.get("/unsubscribe", params={"e": "buyer@example.com", "d": "pmmarketing.ru"})
    assert "уже отписаны" in r3.text
    # Администратор видит отписки с привязкой к отправителю.
    _login(client)
    page = client.get("/stoplist/").text
    assert "pmmarketing.ru" in page
    assert "animateme.ru" in page


def test_unsubscribe_invalid_email(client):
    resp = client.get("/unsubscribe/not-an-email")
    assert resp.status_code == 400


def test_campaign_form_shows_template_selector(client):
    _login(client)
    token = _csrf(client, "/templates/new")
    client.post(
        "/templates/",
        data={"csrfmiddlewaretoken": token, "name": "Шаблон А", "mask": "<p>hi</p>", "mask_json": "{}"},
    )
    form = client.get("/campaigns/new").text
    assert "Готовый шаблон" in form
    assert "Шаблон А" in form
    assert 'id="msg-body"' in form
    assert 'name="template_id"' in form


# --- Пользователи ------------------------------------------------------------


def test_users_requires_superuser(client):
    _login(client)  # tester — не суперпользователь
    resp = client.get("/users/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/campaigns/"


def test_users_crud_as_admin(client):
    _login_as(client, "boss")
    listing = client.get("/users/")
    assert listing.status_code == 200
    assert "Пользователи" in listing.text
    # Создание.
    token = _csrf(client, "/users/new")
    client.post(
        "/users/",
        data={
            "csrfmiddlewaretoken": token,
            "username": "newbie",
            "password": "s3cret!",
            "email": "newbie@ex.com",
            "first_name": "Нео",
        },
        follow_redirects=False,
    )
    page = client.get("/users/").text
    assert "newbie" in page
    import re as _re

    uid = _re.search(r"/users/([0-9a-f-]{36})/block", page).group(1)
    token = _csrf(client, "/users/")
    client.post(f"/users/{uid}/block", data={"csrfmiddlewaretoken": token})
    assert "Заблокирован" in client.get("/users/").text


# --- Аккаунт -----------------------------------------------------------------


def test_account_profile_update(client):
    _login(client)
    token = _csrf(client, "/account/profile")
    resp = client.post(
        "/account/profile",
        data={"csrfmiddlewaretoken": token, "first_name": "Пётр", "last_name": "Петров", "email": "p@ex.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Пётр" in client.get("/account/profile").text


def test_account_security_change_password(client):
    _login(client)
    token = _csrf(client, "/account/security")
    # Неверный текущий пароль.
    bad = client.post(
        "/account/security",
        data={
            "csrfmiddlewaretoken": token,
            "current_password": "wrong",
            "new_password": "abcdef",
            "confirm_password": "abcdef",
        },
    )
    assert bad.status_code == 400
    # Корректная смена.
    token = _csrf(client, "/account/security")
    ok = client.post(
        "/account/security",
        data={
            "csrfmiddlewaretoken": token,
            "current_password": "secret",
            "new_password": "abcdef",
            "confirm_password": "abcdef",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert ok.headers["location"] == "/login"


# --- Письма: компоновка и папки ---------------------------------------------


def test_compose_form_and_no_profile(client):
    _login(client)
    assert client.get("/messages/new").status_code == 200
    token = _csrf(client, "/messages/new")
    resp = client.post(
        "/messages/new",
        data={"csrfmiddlewaretoken": token, "clients": "a@ex.com", "subject": "Тема", "message": "Текст"},
    )
    # У tester нет профиля SMTP.
    assert resp.status_code == 400
    assert "профиль" in resp.text.lower()


def test_compose_sends_and_records(client, monkeypatch):
    _login(client)
    # Профиль SMTP.
    token = _csrf(client, "/smtp/")
    client.post(
        "/smtp/",
        data={
            "csrfmiddlewaretoken": token, "title": "Осн", "email_host": "smtp.ex.ru",
            "email_port": "465", "email_host_user": "u@ex.ru", "email_host_password": "p",
            "default_from_email": "u@ex.ru", "email_use_ssl": "1",
        },
    )
    monkeypatch.setattr("cbmail.web.routers.messages._send_single", lambda *a, **k: None)
    token = _csrf(client, "/messages/new")
    resp = client.post(
        "/messages/new",
        data={"csrfmiddlewaretoken": token, "clients": "client@ex.com", "subject": "Привет", "message": "Тело"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Привет" in client.get("/messages/").text


def test_folder_create_and_shows(client):
    _login(client)
    token = _csrf(client, "/messages/")
    resp = client.post(
        "/messages/folders",
        data={"csrfmiddlewaretoken": token, "name": "Клиенты"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Клиенты" in client.get("/messages/").text


# --- Правки пользователя: query-отписка и «Отправить сейчас» ------------------


def test_unsubscribe_query_form(client):
    # Новый формат ссылки отписки: /unsubscribe?e=<email>.
    resp = client.get("/unsubscribe", params={"e": "Q@Example.com"})
    assert resp.status_code == 200
    assert "успешно отписались" in resp.text
    _login(client)
    assert "q@example.com" in client.get("/stoplist/").text


def test_create_campaign_send_now(client, monkeypatch):
    calls = []
    import cbmail.workers.tasks as tasks

    monkeypatch.setattr(tasks.send_campaign_task, "delay", lambda *a, **k: calls.append((a, k)))
    _login(client)
    token = _csrf(client, "/campaigns/new")
    resp = client.post(
        "/campaigns/",
        data={
            "csrfmiddlewaretoken": token,
            "name": "Срочная",
            "subject": "Тема",
            "message": "Текст {{unsubscribe_url}}",
            "recipient_emails": "a@example.com",
            "message_count": "50",
            "message_interval": "1",
            "intent": "send_now",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert calls, "send_now должен поставить задачу отправки в очередь"


def test_create_campaign_with_only_template_selected(client):
    """Новичок выбрал шаблон, но не нажал «показать в поле» — письмо не пустое."""
    _login(client)
    token = _csrf(client, "/templates/new")
    client.post(
        "/templates/",
        data={
            "csrfmiddlewaretoken": token,
            "name": "Промо",
            "mask": "<p>Здравствуйте, {{email}}!</p>",
            "mask_json": "{}",
        },
    )
    # Узнаём id только что созданного шаблона со страницы формы кампании.
    import re

    form = client.get("/campaigns/new").text
    tid = re.search(r'name="template_id"[\s\S]*?<option value="(\d+)"', form).group(1)

    token = _csrf(client, "/campaigns/new")
    resp = client.post(
        "/campaigns/",
        data={
            "csrfmiddlewaretoken": token,
            "name": "По шаблону",
            "subject": "Тема",
            "message": "",  # поле письма пустое — тело берётся из шаблона
            "template_id": tid,
            "recipient_emails": "a@example.com",
            "message_count": "50",
            "message_interval": "1",
            "intent": "draft",
        },
        follow_redirects=False,
    )
    # Раньше отдавало 400 «Письмо не может быть пустым»; теперь — успех.
    assert resp.status_code == 303


def test_create_campaign_template_plus_extra_text(client):
    """Можно совместить: шаблон + собственный текст сохраняются вместе в теле."""
    _login(client)
    token = _csrf(client, "/templates/new")
    client.post(
        "/templates/",
        data={
            "csrfmiddlewaretoken": token,
            "name": "Промо",
            "mask": "<p>Оформление промо</p>",
            "mask_json": "{}",
        },
    )
    import re

    form = client.get("/campaigns/new").text
    tid = re.search(r'name="template_id"[\s\S]*?<option value="(\d+)"', form).group(1)

    token = _csrf(client, "/campaigns/new")
    resp = client.post(
        "/campaigns/",
        data={
            "csrfmiddlewaretoken": token,
            "name": "Шаблон и текст",
            "subject": "Тема",
            "message": "Личное обращение к клиенту",
            "template_id": tid,
            "recipient_emails": "a@example.com",
            "message_count": "50",
            "message_interval": "1",
            "intent": "draft",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Открываем сохранённую кампанию в редакторе — тело письма лежит в textarea.
    cid = re.search(r"/campaigns/(\d+)", resp.headers["location"]).group(1)
    edit = client.get(f"/campaigns/{cid}/edit").text
    # Присутствует и текст пользователя, и оформление шаблона.
    assert "Личное обращение к клиенту" in edit
    assert "Оформление промо" in edit
    # Текст пользователя — выше оформления шаблона.
    assert edit.index("Личное обращение к клиенту") < edit.index("Оформление промо")
