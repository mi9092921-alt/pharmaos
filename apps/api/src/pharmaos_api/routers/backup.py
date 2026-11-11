"""Owner-only manual backup and restore controls.

The scheduled task and CLI remain the operational baseline. These endpoints
reuse the same service and maintenance lock so the UI cannot create a second,
less-safe backup implementation.
"""

import asyncio
import hashlib
import logging
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.config import default_data_dir, get_settings
from pharmaos_api.db import get_session, reset_engine
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import ApiError, ErrorCode, success_envelope
from pharmaos_api.maintenance import MaintenanceBusyError, maintenance_lock
from pharmaos_api.maintenance_state import maintenance_operation
from pharmaos_api.models import User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import audit_service, auth_service, backup_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/system/backup", tags=["backup"])

_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
_RESTORE_CONFIRMATION = "RESTORE_BACKUP"


def _backup_dir() -> Path:
    return backup_service.default_backup_dir()


def _run_manual_backup() -> dict[str, object]:
    try:
        with maintenance_lock(), maintenance_operation("backup.create"):
            return backup_service.create_backup_job(_backup_dir(), cloud=True)
    except MaintenanceBusyError as exc:
        raise ApiError(ErrorCode.BACKUP_BUSY, 409) from exc


def _save_upload(upload: UploadFile, destination: Path) -> tuple[int, str]:
    """Copy an uploaded file without buffering it all in memory."""

    total = 0
    digest = hashlib.sha256()
    with destination.open("wb") as output:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_UPLOAD_BYTES:
                raise ApiError(ErrorCode.BACKUP_INVALID, 413)
            output.write(chunk)
            digest.update(chunk)
    os.chmod(destination, 0o600)
    return total, digest.hexdigest()


def _run_restore(backup_file: Path) -> dict[str, object]:
    try:
        with maintenance_lock(), maintenance_operation("backup.restore"):
            return backup_service.restore_to_cluster(
                backup_file,
                pgdata_dir=default_data_dir() / "pgdata",
                live_port=get_settings().db_port,
            )
    except MaintenanceBusyError as exc:
        raise ApiError(ErrorCode.BACKUP_BUSY, 409) from exc
    except backup_service.BackupError as exc:
        logger.warning("owner restore rejected: %s", exc)
        raise ApiError(ErrorCode.BACKUP_INVALID, 422) from exc


async def _record_audit(
    action: str,
    actor: User,
    *,
    metadata: dict[str, object],
) -> None:
    try:
        await audit_service.record_independent(action, actor=actor, metadata=metadata)
    except Exception:
        # The operation result is still truthful, but the failure is loud in
        # the device log. A restored database may not contain the exact actor
        # row from the pre-restore database, so retry as a system event rather
        # than losing the audit record to a foreign-key failure.
        logger.exception("could not record %s audit event", action)
        try:
            await audit_service.record_independent(
                action,
                metadata={**metadata, "actor_username": actor.username},
            )
        except Exception:
            logger.exception("could not record fallback %s audit event", action)


@router.post("/create")
async def create_manual_backup(
    request: Request,
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_permission("settings.backup")),
) -> dict[str, object]:
    enforce_csrf(request)
    try:
        result = await asyncio.to_thread(_run_manual_backup)
    except ApiError:
        raise
    except Exception as exc:
        logger.exception("manual backup failed")
        raise ApiError(ErrorCode.BACKUP_FAILED, 500) from exc

    await _record_audit(
        AuditAction.BACKUP_CREATED,
        current_user,
        metadata={"file_name": result["file_name"], "size_bytes": result["size_bytes"]},
    )
    return success_envelope(result)


@router.post("/restore")
async def restore_backup(
    request: Request,
    response: Response,
    password: str = Form(..., min_length=1, max_length=128),
    confirmation: str = Form(..., min_length=1, max_length=64),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_permission("settings.backup")),
) -> dict[str, object]:
    enforce_csrf(request)
    if confirmation != _RESTORE_CONFIRMATION:
        raise ApiError(ErrorCode.BACKUP_INVALID, 422)
    filename = Path(file.filename or "").name
    if not filename.lower().endswith(backup_service.BACKUP_SUFFIX):
        raise ApiError(ErrorCode.BACKUP_INVALID, 422)

    try:
        verified_user = await auth_service.authenticate(session, current_user.username, password)
    except ApiError as exc:
        raise ApiError(ErrorCode.BACKUP_RESTORE_REAUTH, 401) from exc
    if verified_user.id != current_user.id:
        raise ApiError(ErrorCode.BACKUP_RESTORE_REAUTH, 401)

    # Release the request's old DB connection before the cluster-level swap.
    await session.close()
    fd, temp_name = tempfile.mkstemp(
        prefix="pharmaos-restore-", suffix=backup_service.BACKUP_SUFFIX
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        size_bytes, sha256 = await asyncio.to_thread(_save_upload, file, temp_path)
        try:
            report = await asyncio.to_thread(_run_restore, temp_path)
        finally:
            await reset_engine()
    except ApiError:
        raise
    except Exception as exc:
        logger.exception("owner restore failed")
        raise ApiError(ErrorCode.BACKUP_FAILED, 500) from exc
    finally:
        await asyncio.to_thread(temp_path.unlink, missing_ok=True)
        await file.close()

    await _record_audit(
        AuditAction.BACKUP_RESTORED,
        current_user,
        metadata={"file_name": filename, "size_bytes": size_bytes, "sha256": sha256},
    )
    settings = get_settings()
    for cookie_name in (
        settings.access_cookie_name,
        settings.refresh_cookie_name,
        settings.csrf_cookie_name,
    ):
        response.delete_cookie(cookie_name, path="/")
    return success_envelope({"file_name": filename, "report": report})
