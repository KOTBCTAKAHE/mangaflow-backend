# MangaFlow Backend

Backend для мобильной читалки [MangaFlow](https://github.com/KOTBCTAKAHE/mangaflow-app): русскоязычные источники манги, манхвы и маньхуа, библиотека пользователя, история, прогресс чтения, синхронизация между устройствами и прокси изображений.

**Стек:** Python 3.12, FastAPI, PostgreSQL 16, Redis 7, Celery, SQLAlchemy 2 (async), Alembic, Docker Compose, nginx (edge-кэш изображений).

```
.
├── source_adapters/     # пакет mangaflow-sources — Source Adapter Layer (9 источников)
├── backend/             # FastAPI-приложение, миграции, Celery, тесты
├── infrastructure/      # Dockerfile, docker-compose, nginx
└── docs/                # архитектура, ADR, отчёт по источникам, API
```

## Быстрый старт (Docker)

```bash
cd infrastructure
cp .env.example .env          # поменяйте SECRET_KEY и POSTGRES_PASSWORD
docker compose up -d --build
curl http://localhost:8080/health/ready
open http://localhost:8080/docs   # Swagger / OpenAPI
```

Поднимаются `api` (миграции применяются автоматически), `worker` и `beat` (Celery: проверка новых глав и очистка сессий), `postgres`, `redis`, `nginx`.

### Обновление сервера

После каждого коммита в `main` (когда тесты прошли) GitHub Actions публикует готовый образ
`ghcr.io/kotbctakahe/mangaflow-backend:latest` (amd64 и arm64). Собирать на сервере ничего не нужно:

```bash
cd infrastructure
./update.sh                    # = docker compose pull && docker compose up -d
```

Откат на конкретную сборку: `MANGAFLOW_IMAGE=ghcr.io/kotbctakahe/mangaflow-backend:sha-<коммит>` в `.env`, затем `./update.sh`.
Для локальной разработки по-прежнему работает `docker compose up -d --build`.

> ⚠️ **Гео-блокировки.** Большинство сайтов-источников отдают 403 или заглушку для не-российских IP. Если сервер стоит за рубежом, укажите `SOURCES_PROXY` (HTTP или SOCKS5 с RU-выходом) в `.env`. Состояние каждого источника видно в `GET /api/v1/sources/{id}/health`.

## Локальная разработка

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e "source_adapters[test]" -e "backend[test]"
createdb mangaflow && createdb mangaflow_test   # или поднимите postgres из compose
cd backend
export DATABASE_URL=postgresql+asyncpg://mangaflow:mangaflow@localhost:5432/mangaflow
alembic upgrade head
uvicorn app.main:app --reload
celery -A app.workers.celery_app worker -B -l info   # в отдельном терминале
```

Тесты:

```bash
pytest -q source_adapters/tests                       # адаптеры (на фикстурах, сеть не нужна)
TEST_DATABASE_URL=postgresql+asyncpg://...mangaflow_test pytest -q backend/tests
```

Для локального запуска без Redis подойдёт `REDIS_URL=memory://` (кэш и rate limit в памяти процесса).

## API

Все эндпоинты находятся под `/api/v1`, полная схема доступна в `/docs` и `/openapi.json`. Обзор — в [docs/API.md](docs/API.md).

| Группа | Назначение |
|---|---|
| `/auth` | регистрация, вход, refresh с ротацией, выход |
| `/users` | профиль, настройки (LWW), сессии, статистика, экспорт и импорт бэкапа |
| `/sources` | список источников, их возможности, жанры, health |
| `/manga` | каталог с фильтрами, тайтл с главами, подборки для главного экрана |
| `/chapters` | страницы главы (подписанные ссылки на прокси) |
| `/search` | глобальный поиск по всем источникам с объединением дублей, история запросов |
| `/library`, `/collections` | библиотека, статусы, избранное, коллекции, delta-синхронизация |
| `/progress`, `/history` | прогресс по главам, «продолжить чтение», сессии чтения |
| `/downloads` | реестр офлайн-загрузок по устройствам, пакетный манифест страниц |
| `/updates` | лента новых глав, ручной запуск проверки |
| `/images/{token}` | CDN-совместимый прокси изображений |

Ошибки всегда приходят в одном формате:

```json
{"error": {"code": "source_blocked", "message": "...", "details": {"source_id": "remanga"}, "request_id": "..."}}
```

## Конфигурация

Все параметры читаются из переменных окружения (`app/core/config.py`). Основные:

| Переменная | По умолчанию | |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://mangaflow:mangaflow@localhost:5432/mangaflow` | |
| `REDIS_URL` | `redis://localhost:6379/0` | `memory://` для dev |
| `SECRET_KEY` | — | **обязательно** в production |
| `PUBLIC_BASE_URL` | пусто | внешний адрес API для ссылок на картинки |
| `SOURCES_PROXY` | — | прокси для запросов к источникам |
| `SOURCES_DISABLED` | `[]` | JSON-список id |
| `SOURCE_<ID>_BASE_URL` / `_TOKEN` / `_COOKIE` / `_PROXY` / `_EXTRA_<KEY>` | — | зеркало и собственные учётные данные оператора для конкретного источника |
| `UPDATE_CHECK_INTERVAL_MINUTES` | `360` | период проверки новых глав |

## Источники

Перенесены на Python из [redcatpet/mangayomi-ru-sources](https://github.com/redcatpet/mangayomi-ru-sources) (Apache-2.0): **MangaLib, Remanga, Senkuro, ReadManga, MintManga (18+), SelfManga, Desu, MangaBuff, Acomics**. Подробности и отчёт об исследовании — в [docs/SOURCES.md](docs/SOURCES.md), архитектура — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Новый источник — это один класс-наследник `SourceAdapter`. Встроенный регистрируется в `BUILTIN_ADAPTERS`, внешний пакет — через entry point `mangaflow.sources`, без правок приложения.

Сервис не обходит авторизацию, пейволлы и защиту сайтов: платные и непубличные главы скрываются, 403 отображается как ошибка источника.

## Лицензия и атрибуция

Логика адаптеров основана на коде `redcatpet/mangayomi-ru-sources` (Apache License 2.0).
