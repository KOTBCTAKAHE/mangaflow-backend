from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "MangaFlow API"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True

    # Public base URL used to build image-proxy links (e.g. https://api.example.com).
    # When empty the request's own base URL is used.
    public_base_url: str = ""
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    database_url: str = "postgresql+asyncpg://mangaflow:mangaflow@localhost:5432/mangaflow"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = ""

    secret_key: str = "change-me-in-production-please-32+chars"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    # A rotated refresh token presented again within this window is a benign race
    # (two requests/processes of the same app refreshing at once), not theft.
    refresh_reuse_grace_seconds: int = 120
    image_url_ttl_hours: int = 72

    # Source layer
    source_timeout: float = 15.0
    source_max_retries: int = 2
    source_rate_per_second: float = 4.0
    source_search_timeout: float = 12.0
    sources_disabled: list[str] = Field(default_factory=list)
    sources_proxy: str | None = None  # e.g. socks5://ru-exit:1080 for geo-blocked sites

    # Cache TTLs (seconds)
    cache_ttl_catalog: int = 600
    cache_ttl_details: int = 1800
    cache_ttl_chapters: int = 600
    cache_ttl_pages: int = 3600
    cache_ttl_search: int = 300

    # Rate limiting (requests per window)
    rate_limit_enabled: bool = True
    rate_limit_default: int = 240
    rate_limit_auth: int = 20
    rate_limit_window_seconds: int = 60

    # Image proxy
    image_max_bytes: int = 15 * 1024 * 1024
    image_cache_dir: str = "/tmp/mangaflow-images"
    image_cache_max_mb: int = 2048

    update_check_interval_minutes: int = 360

    # «Найти по скриншоту»: free key from https://saucenao.com/user.php?page=search-api
    saucenao_api_key: str = ""

    # Our own reverse image search (service `imgindex`): pages of our sources → SSCD vectors.
    imgindex_url: str = ""  # API side, e.g. http://imgindex:8100; empty = engine off
    imgindex_dir: str = "/var/lib/mangaflow-index"
    imgindex_model_url: str = ("https://github.com/KOTBCTAKAHE/mangaflow-backend/releases/download/"
                               "model-sscd-v1/sscd_disc_mixup.onnx")
    imgindex_threads: int = 2  # CPU threads for the model; the API keeps the rest
    imgindex_max_vectors: int = 1_500_000  # ≈1 GB RAM (int8); the crawler stops at the cap
    imgindex_crawl: bool = True  # index popular titles in the background, not only what people read
    imgindex_pages_per_minute: int = 40  # politeness towards sources (crawler only)
    imgindex_titles_per_source: int = 300  # popular titles to cover per source
    imgindex_chapters_per_title: int = 30  # chapters per title, spread over the whole run
    # plot search + «Для вас»: descriptions → multilingual-e5-small vectors (same service)
    imgindex_text_model_url: str = ("https://github.com/KOTBCTAKAHE/mangaflow-backend/releases/download/"
                                    "model-e5-small-v1/e5_small_int8.onnx")
    imgindex_plot: bool = True
    imgindex_plot_titles_per_source: int = 3000  # how deep into each catalogue (popular first)
    imgindex_plot_titles_per_minute: int = 30  # title-page requests for descriptions

    # Trackers: register an OAuth app on each service with the redirect URI
    # <PUBLIC_BASE_URL>/api/v1/trackers/<service>/callback
    shikimori_client_id: str = ""
    shikimori_client_secret: str = ""
    shikimori_base_url: str = "https://shikimori.io"  # .one answers 301 since 2026 — breaks the OAuth token POST
    anilist_client_id: str = ""
    anilist_client_secret: str = ""
    mal_client_id: str = ""
    mal_client_secret: str = ""

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
