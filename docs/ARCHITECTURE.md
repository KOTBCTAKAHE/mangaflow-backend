# Архитектура MangaFlow

```
┌──────────────── Flutter app (mangaflow-app) ────────────────┐
│ UI (screens/widgets) → controllers (Riverpod) → repositories│
│        ↘ local SQLite (offline-first)   ↘ ApiClient (Dio)   │
│        ↘ file storage (downloaded pages)  ↘ sync engine     │
└───────────────────────────────┬─────────────────────────────┘
                                │ HTTPS, JWT
┌──────────── nginx (proxy_cache /api/v1/images) ─────────────┐
└───────────────────────────────┬─────────────────────────────┘
┌──────────────────────── FastAPI (api) ──────────────────────┐
│ api/v1 routers → services → repositories → PostgreSQL       │
│           ↘ SourceService (cache) → mangaflow_sources        │
│           ↘ SearchService (fan-out, fuzzy merge)             │
│           ↘ ImageProxy (signed tokens, disk cache, resize)   │
└───────┬───────────────────────────────┬─────────────────────┘
        │ Redis (cache, rate limit, broker)                    
┌───────┴──────── Celery worker + beat ───────────────────────┐
│ check_updates: новые главы для тайтлов библиотеки           │
│ purge_sessions: чистка истёкших refresh-сессий              │
└─────────────────────────────────────────────────────────────┘
```

## Слои backend

| Слой | Ответственность |
|---|---|
| `api/v1/*` | HTTP: валидация (Pydantic), зависимости, коды ответов. Без бизнес-логики. |
| `services/*` | Бизнес-правила: синхронизация (LWW), auth и ротация токенов, поиск, подборки, статистика, бэкапы, проверка обновлений. |
| `repositories/*` | Запросы SQLAlchemy, изоляция по `user_id`. |
| `models/*` | ORM-схема. |
| `core/*` | Конфиг, ошибки, логирование, безопасность, кэш, rate limit. |
| `mangaflow_sources` | Независимый пакет без зависимостей от FastAPI и БД. |

## Модель данных

- `users`, `refresh_sessions` (ротация, `family_id` для обнаружения повторного использования токена).
- `library_entries` — **снимок** тайтла (название, обложка) плюс пользовательские поля. Сам каталог в БД не дублируется.
- `collections`, `collection_items`.
- `reading_progress` — по одной строке на главу; `completed` монотонен.
- `history_entries` — сессии чтения, `id` генерирует клиент (идемпотентная загрузка). Отсюда считается статистика и календарь активности.
- `device_downloads` — что и на каком устройстве загружено.
- `tracked_titles` (общий для всех пользователей) и `chapter_updates` (лента у каждого пользователя свой).
- `search_history`.

## Синхронизация (offline-first)

1. Клиент пишет всё в локальную SQLite и складывает изменения в очередь (`dirty`).
2. При появлении сети клиент выполняет push пачками: `POST /library/sync`, `POST /progress/batch`, `POST /history`, `PUT /users/me/settings`.
3. Затем pull: `GET /library?since=…`, `GET /progress?since=…`, `GET /history?since=…`. Метка `server_time` из ответа сохраняется до следующего раза.
4. Конфликты решаются по правилу **last-write-wins** по `updated_at` клиента. Удаление передаётся как tombstone (`deleted: true`). Отметка «глава прочитана» никогда не теряется.

## Безопасность

- Пароли хэшируются Argon2id; при неверном логине время ответа одинаковое (dummy hash).
- Access JWT живёт 15 минут. Refresh-токен непрозрачный, в БД хранится его SHA-256, при каждом обновлении выпускается новый. Если старый токен предъявлен повторно, отзывается вся семья сессий.
- Rate limit: фиксированное окно в Redis по пользователю или IP; для `/auth` лимит строже.
- Прокси изображений не открытый: принимает только URL, подписанные HMAC самим сервером, с TTL (обложки — год, страницы — 72 ч). Проверяются размер и MIME.
- Все пользовательские запросы фильтруются по `user_id`; изоляция покрыта тестами.

## Решения (ADR)

См. [`docs/adr`](adr).
