# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY source_adapters /srv/source_adapters
RUN pip install /srv/source_adapters

COPY backend/pyproject.toml /srv/backend/pyproject.toml
COPY backend/app /srv/backend/app
RUN pip install /srv/backend

COPY backend/alembic.ini /srv/backend/alembic.ini
COPY backend/migrations /srv/backend/migrations
COPY infrastructure/entrypoint.sh /srv/entrypoint.sh
RUN chmod +x /srv/entrypoint.sh && useradd -r -u 10001 mangaflow && mkdir -p /var/cache/mangaflow /var/lib/mangaflow-index && chown mangaflow /var/cache/mangaflow /var/lib/mangaflow-index

WORKDIR /srv/backend
USER mangaflow
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://localhost:8000/health/live || exit 1
ENTRYPOINT ["/srv/entrypoint.sh"]
CMD ["api"]
