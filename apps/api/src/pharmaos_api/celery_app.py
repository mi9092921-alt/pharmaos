"""Celery application (CLAUDE.md stack: Celery 5 + Redis 7).

Runs the DAILY encrypted backup (+ one-way cloud copy) via beat.
The backup hour is configuration (default 02:00 local device time).
"""

import logging
import os
from pathlib import Path

from celery import Celery
from celery.schedules import crontab

from pharmaos_api.config import get_settings

logger = logging.getLogger(__name__)

celery = Celery("pharmaos", broker=get_settings().redis_url, backend=None)
celery.conf.timezone = os.environ.get("PHARMAOS_TZ", "Africa/Cairo")

BACKUP_HOUR = int(os.environ.get("BACKUP_HOUR", "2"))


@celery.task(name="pharmaos.backup.daily")
def daily_backup_task() -> str:
    """Create the daily encrypted backup and push the one-way cloud copy."""
    from pharmaos_api.services import backup_service

    backup_dir = Path(os.environ.get("BACKUP_PATH", "/var/pharmaos/backups"))
    backup_file = backup_service.create_backup(backup_dir)
    uploaded = backup_service.upload_to_cloud(backup_file)
    if not uploaded:
        logger.warning("Daily backup stored locally only — cloud copy is not configured.")
    return str(backup_file)


celery.conf.beat_schedule = {
    "daily-encrypted-backup": {
        "task": "pharmaos.backup.daily",
        "schedule": crontab(hour=BACKUP_HOUR, minute=0),
    }
}
