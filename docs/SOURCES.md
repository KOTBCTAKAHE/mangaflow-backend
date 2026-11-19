# Источники: исследование и интеграция

## 1. Что такое `redcatpet/mangayomi-ru-sources`

- Репозиторий **расширений для Mangayomi** (Flutter-приложение), а не библиотека и не сервис. Лицензия **Apache-2.0**.
- 30 источников: 12 манга, 12 аниме, 6 новелл. Каждый источник — JS-файл с классом `DefaultExtension extends MProvider`.
- Код опирается на рантайм Mangayomi: `Client` (HTTP), `Document` (HTML-парсер с CSS-селекторами), `SharedPreferences` (настройки), `MProvider`. Вне Mangayomi он не исполняется. **REST API у репозитория нет.**
- Общие базы, которые подставляются в файлы источников на этапе сборки (`build.py`, директива `// @include:`):
  - `lib/grouple_base.js` — HTML-движок Grouple (ReadManga / MintManga / SelfManga / AllHentai). Страницы главы извлекаются из inline-вызова `readerInit(...)`.
  - `lib/lib_family_base.js` — JSON API семейства Lib (`api2.mangalib.me`, заголовок `Site-Id`), выбор CDN изображений.
- Отдельные источники: Remanga (REST v2), Senkuro / Senkognito (GraphQL), Desu (REST), MangaBuff (HTML), Acomics (HTML).
- По README автора, с EU IP реальный контент отдают 24/30 источников, с RU IP — больше. Для 18+ и платных глав нужен Bearer-токен или cookie пользователя.

## 2. Решение

| Вариант | Плюсы | Минусы |
|---|---|---|
| Исполнять JS в QuickJS или Node с эмуляцией API Mangayomi | можно подтягивать обновления автора как есть | нужно эмулировать `Document`, `Client` и `SharedPreferences`; хрупко; две среды исполнения в бэкенде; сложно тестировать |
| **Перенести логику на Python (выбрано)** | единый язык и стек, типизированные модели, нормальные тесты, контроль таймаутов, retries и rate limit | при изменении вёрстки сайтов правки переносятся вручную |

Логика парсинга (селекторы, эндпоинты, GraphQL-запросы, особенности вроде `Site-Id`, CDN-фоллбэков и заголовков Referer) **перенесена 1-в-1** с атрибуцией в каждом модуле. Перенос — производная работа под Apache-2.0.

## 3. Перенесённые источники

| id | Сайт | Тип API | NSFW | Возможности | Примечания |
|---|---|---|---|---|---|
| `mangalib` | mangalib.org | JSON (`api2.mangalib.me`) | – | поиск, жанры (вкл/искл), тип, статус, 4 сортировки | автовыбор CDN через `/constants`; 18+ главы только с токеном оператора |
| `remanga` | remanga.org | JSON v2 | – | поиск, тип, статус, 4 сортировки | платные непокупленные главы **скрываются** |
| `senkuro` | senkuro.com | GraphQL | – | поиск по всем локализациям, жанры (вкл/искл), тип, статус | курсорная пагинация |
| `readmanga` | readmanga | HTML (Grouple) | – | поиск, жанр, 4 сортировки | домен меняется → `SOURCE_READMANGA_BASE_URL` |
| `mintmanga` | mintmanga | HTML (Grouple) | 18+ | как readmanga | по умолчанию скрыт (`include_nsfw=false`) |
| `selfmanga` | selfmanga | HTML (Grouple) | – | как readmanga | русская авторская манга |
| `desu` | desu.uno | JSON (`/api/manga`) | – | поиск, тип, статус | домен менялся: desu.me → desu.city → desu.uno |
| `mangabuff` | mangabuff.ru | HTML | – | поиск, 3 сортировки | |
| `acomics` | acomics.ru | HTML | – | поиск | веб-комиксы: 1 выпуск = 1 страница |
| `mangapoisk` | mangapoisk.me | HTML | – | поиск (от 3 символов), жанры (вкл/искл), статус, 4 сортировки | порт из keiyoushi/extensions-source (Apache-2.0); домен меняется → `SOURCE_MANGAPOISK_BASE_URL` |
| `mangadex` | mangadex.org (**EN**) | официальный JSON API | – | поиск, жанры (вкл/искл), тип, статус, 5 сортировок | не из redcatpet; язык перевода — `SOURCE_MANGADEX_EXTRA_LANG` (по умолчанию `en`); внешние главы (MANGA Plus и т. п.) скрываются; картинки с `uploads.mangadex.org` |
| `mangadex_ru` | mangadex.org (**RU**) | официальный JSON API | – | как `mangadex` | тот же каталог, только русские переводы |
| `seimanga` | seimanga | HTML (Grouple) | – | как readmanga | зеркало → `SOURCE_SEIMANGA_BASE_URL` |
| `usagi` | usagi.one | HTML (Grouple) | – | как readmanga | |
| `astramanga` | astramanga.org | JSON (`api.astramanga.org/api/v1`) | – | поиск, тип, статус, 5 сортировок | ветки переводов объединяются; порт из keiyoushi |
| `tomilolib` | tomilo-lib.ru | JSON (`/api`) | – | поиск, 5 сортировок | 18+ тайтлы скрыты (`SOURCE_TOMILOLIB_EXTRA_SHOW_ADULT=1`); платные главы помечены 🔒; порт из keiyoushi |
| `mangapill` | mangapill.com (**EN**) | HTML | – | поиск | английские сканы; порт из keiyoushi |
| `yaoilib` | shlib.life | JSON (`api2.mangalib.me`, Site-Id 2) | 18+ | как mangalib | сеть MangaLib; токен → `SOURCE_YAOILIB_TOKEN` |
| `allhentai` | allhen.online | HTML (Grouple) | 18+ | как readmanga | часть глав — только с cookie оператора (`SOURCE_ALLHENTAI_COOKIE`) |
| `hentailib` | hentailib.me | JSON (`api2.mangalib.me`, Site-Id 4) | 18+ | как mangalib | почти всё — только с токеном оператора (`SOURCE_HENTAILIB_TOKEN`) |

**Не перенесены / убраны:** аниме и новеллы, Senkognito, HenChan, ComX (защита DLE Guard), InkStory/PureManga (Seroval-стриминг); MangaMen (403 для серверов дата-центров), Manga-Chan и Yaoi-Chan (CDN картинок недоступен, сайт отвечает 502/таймаутом). Живая проверка всех источников: `python source_adapters/tools/live_check.py [id ...]` (или workflow «Live sources check»). 18+ источники по умолчанию скрыты (`include_nsfw=false`).

## 4. Source Adapter Layer

```
mangaflow_sources/
├── models.py     # MangaSummary, MangaDetails, Chapter, PageImage, SearchQuery, SourceInfo, enums
├── base.py       # SourceAdapter (ABC) + SourceConfig
├── http.py       # SourceHttpClient: token-bucket rate limit, retries+backoff, Retry-After, маппинг ошибок
├── errors.py     # SourceUnavailable / Blocked / AuthRequired / NotFound / RateLimited / Parse
├── ids.py        # base64url-кодирование ссылок источника в публичные id
├── registry.py   # SourceRegistry + entry points "mangaflow.sources"
└── adapters/     # реализации
```

Контракт адаптера:

```python
class SourceAdapter(ABC):
    info: ClassVar[SourceInfo]               # id, имя, возможности, жанры
    async def popular(page) -> MangaListPage
    async def latest(page) -> MangaListPage
    async def search(SearchQuery) -> MangaListPage   # текст + жанры вкл/искл + типы + статусы + сортировка
    async def details(manga_id) -> MangaDetails
    async def chapters(manga_id) -> list[Chapter]    # новые первыми
    async def pages(chapter_id) -> list[PageImage]
    async def title(manga_id)                        # details+chapters за один запрос (HTML-источники переопределяют)
    def image_headers(url) -> dict                   # Referer/Origin для CDN — использует прокси изображений
```

Сквозные механизмы:

- **Таймауты:** connect ≤ 8 с, общий 15 с; в глобальном поиске каждый источник дополнительно ограничен `SOURCE_SEARCH_TIMEOUT`.
- **Retries:** до 2 повторов с экспоненциальным backoff и jitter на сетевые ошибки, 429, 5xx и коды Cloudflare 52x. `Retry-After` учитывается.
- **Rate limit:** token bucket на каждый источник (по умолчанию 4 запроса/с).
- **Кэш (Redis):** каталог 10 мин, тайтл 30 мин, страницы 1 ч, поиск 5 мин.
- **Недоступность:** ошибка одного источника не ломает поиск и главный экран. Она возвращается в поле `errors[]`, а `/sources/{id}/health` показывает текущее состояние.

## 5. Ограничения, о которых нужно знать

1. **Из среды разработки сайты недоступны** (исходящие запросы закрыты allowlist'ом). Поэтому адаптеры покрыты тестами на фикстурах, воспроизводящих разметку и JSON из оригинальных расширений. Живую проверку нужно делать на сервере, желательно с российским IP: `GET /api/v1/sources/{id}/health?force=true`.
2. Вёрстка HTML-источников (Grouple, MangaBuff, Acomics) со временем меняется. При ошибке парсинга адаптер бросает `SourceParseError`, клиент видит понятное сообщение, а в логах остаётся `source_error`.
3. Токены и cookie, которые принимают адаптеры, — это **учётные данные оператора** сервера, заданные через переменные окружения. Сервис не обходит авторизацию и не вскрывает платный контент.
