# GN Mailer (cbmail)

Почтовая рассылка на **FastAPI + SQLAlchemy** — замена прежней Django-версии.
Веб-панель, кампании массовой рассылки, шаблоны писем, профили SMTP,
адресная отписка (по отправителю), стоп-лист.

## Структура

```
app/                     — приложение и тесты
  cbmail/                — исходный код (web, domain, services, workers, db, smtp)
  tests/                 — unit / integration / web (pytest)
  tools/devserver.py     — локальный запуск на SQLite (127.0.0.1:8011, admin/dev)
  pyproject.toml         — зависимости
_config/cbmail/          — Dockerfile и entrypoint-скрипты
docker-compose.cbmail.yaml — сервисы cbmail-web (8002) и cbmail-worker
.github/workflows/       — CI (тесты) и CD (деплой на прод)
```

## Локальная разработка

```bash
cd app
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install ".[dev]"
pytest -q                      # тесты
ruff check .                   # линтер
python tools/devserver.py      # демо-сервер на http://127.0.0.1:8011 (admin/dev)
```

## CI/CD

- **CI** — на каждый push и pull request: `ruff` + `pytest`.
- **CD** — при push в `main` (или вручную «Run workflow»), только после зелёных
  тестов: код копируется на сервер, образ пересобирается, поднимаются
  `cbmail-web` и `cbmail-worker` (порт 8002), проверяется `/health`.

### Секреты репозитория (Settings → Secrets → Actions)

| Секрет         | Назначение                          |
|----------------|-------------------------------------|
| `SSH_HOST`     | адрес сервера                       |
| `SSH_PORT`     | порт SSH                            |
| `SSH_USER`     | пользователь SSH                    |
| `SSH_PASSWORD` | пароль SSH                          |
| `DEPLOY_PATH`  | каталог на сервере (по умолчанию `/opt/emails-code`) |

Прод: `http://81.177.139.113:8002`.

> Файлы `.env` и `license.key` в репозиторий не входят — они постоянно лежат
> на сервере и деплоем не затрагиваются.
