"""Celery application (CLAUDE.md stack: Celery 5 + Redis 7).

Cloud-worker path ONLY — the desktop device runtime does not install
celery/redis (moved to the `cloud` optional extra; the device schedules the
daily backup via Windows Task Scheduler + the CLI instead). The API never
imports this module, so PyInstaller never bundles it.

Runs the DAILY encrypted backup (+ one-way cloud copy) via beat.
The backup hour is configuration (default 02:00 local device time).
"""

import logging
import os
from pathlib import Path

from celery import Celery
from celery.schedules import crontab

logger = logging.getLogger(__name__)

# REDIS_URL lives here (not in config.py): nothing in the desktop runtime
# references Redis, so the setting belongs to the cloud worker only.
celery = Celery(
    "pharmaos", broker=os.environ.get("REDIS_URL", "redis://localhost:6379"), backend=None
)
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
