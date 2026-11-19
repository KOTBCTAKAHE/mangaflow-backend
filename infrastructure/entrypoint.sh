#!/bin/sh
set -e
case "$1" in
  api)
    alembic upgrade head
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --workers "${WEB_CONCURRENCY:-2}"
    ;;
  worker)
    exec celery -A app.workers.celery_app worker -l "${LOG_LEVEL:-info}" --concurrency "${WORKER_CONCURRENCY:-2}"
    ;;
  beat)
    exec celery -A app.workers.celery_app beat -l "${LOG_LEVEL:-info}" --schedule /tmp/celerybeat-schedule
    ;;
  imgindex)
    # one process: it owns the vector index in memory
    exec uvicorn app.imgindex.main:app --host 0.0.0.0 --port 8100 --workers 1
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    exec "$@"
    ;;
esac
