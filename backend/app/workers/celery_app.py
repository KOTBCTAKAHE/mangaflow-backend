"""Celery worker + beat schedule.

Run::

    celery -A app.workers.celery_app worker -l info
    celery -A app.workers.celery_app beat -l info
"""
from __future__ import annotations

from celery import Celery

from ..core.config import get_settings

settings = get_settings()

celery = Celery("mangaflow", broker=settings.broker_url, backend=settings.redis_url, include=["app.workers.tasks"])
celery.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=60 * 30,
    task_soft_time_limit=60 * 25,
    timezone="UTC",
    beat_schedule={
        "check-library-updates": {
            "task": "mangaflow.check_updates",
            "schedule": settings.update_check_interval_minutes * 60,
        },
        "purge-expired-sessions": {
            "task": "mangaflow.purge_sessions",
            "schedule": 24 * 3600,
        },
    },
)
