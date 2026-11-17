# API v1 — обзор

Полная интерактивная схема: `/docs` (Swagger) и `/openapi.json`.

## Аутентификация
`Authorization: Bearer <access_token>`. Access живёт 15 минут, refresh 30 дней с ротацией.

| Метод | Путь | |
|---|---|---|
| POST | `/auth/register` | `{email, username, password, display_name?, device_name?}` → `{user, tokens}` |
| POST | `/auth/login` | `{login (email\|username), password, device_name?}` |
| POST | `/auth/refresh` | `{refresh_token}` → новая пара (старый refresh становится недействительным) |
| POST | `/auth/logout` | `{refresh_token}` |
| POST | `/auth/logout-all` | 🔒 |

## Каталог (без авторизации; с токеном доступны персональные подборки)
| Метод | Путь | |
|---|---|---|
| GET | `/sources?include_nsfw=` | источники, возможности, жанры |
| GET | `/sources/{id}/health?force=` | доступность источника |
| GET | `/manga/{source}?q=&page=&sort=popular\|rating\|updated\|created\|name&genres=&exclude_genres=&types=manga\|manhwa\|manhua\|comic&statuses=` | каталог |
| GET | `/manga/{source}/feed/home` | подборки: популярное, обновления, новое, рекомендации |
| GET | `/manga/{source}/{manga_id}?refresh=` | тайтл и главы |
| GET | `/manga/{source}/{manga_id}/chapters?order=asc\|desc` | |
| GET | `/chapters/{source}/{chapter_id}/pages` | подписанные URL страниц |
| GET | `/search?q=&page=&sources=&types=&statuses=&genres=&exclude_genres=&include_nsfw=` | глобальный поиск. `genres`/`exclude_genres` — **названия** жанров (у каждого источника свои id, сервер сопоставляет их сам). `items[].sources[]` — где найден тайтл, `errors[]` — какие источники не ответили |
| GET/DELETE | `/search/history` | |
| GET | `/images/{token}?w=160\|320\|480\|720\|1080` | прокси изображений |

## Пользовательские данные 🔒
| Метод | Путь | |
|---|---|---|
| GET/PATCH/DELETE | `/users/me` | профиль; удаление требует `?password=` |
| POST | `/users/me/password` | смена пароля, остальные сессии завершаются |
| GET/PUT | `/users/me/settings` | LWW по `updated_at` |
| GET/DELETE | `/users/me/sessions[/{id}]` | устройства |
| GET | `/users/me/stats?days=` | прочитанные главы, время, серии, календарь активности |
| GET/POST | `/users/me/export`, `/users/me/import` | бэкап `mangaflow-backup` v1 (импорт — слияние) |
| GET | `/library?since=&status=&favorite=&collection_id=&q=&sort=` | `since` возвращает дельту вместе с tombstones |
| PUT | `/library` | upsert одного тайтла |
| POST | `/library/sync` | пачка (LWW) |
| GET/PATCH/DELETE | `/library/{source}/{manga_id}` | |
| CRUD | `/collections`, `/collections/{id}/items` | |
| GET/PUT | `/progress`, POST `/progress/batch`, GET `/progress/continue` | |
| GET/POST/DELETE | `/history` | |
| GET/POST | `/downloads`, DELETE `/downloads/devices/{id}`, POST `/downloads/manifest` | |
| GET | `/updates?unseen=&since=` · POST `/updates/seen` · POST `/updates/check` | |

## Health
`GET /health/live`, `GET /health/ready` (БД + Redis).
