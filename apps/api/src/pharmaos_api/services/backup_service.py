"""Encrypted database backup, one-way cloud copy, and restore drill (CLAUDE.md).

Design:
- pg_dump (custom format) + an EMERGENCY COPY of the keystore keys are bundled
  into a tar archive, encrypted as one AES-256-GCM blob with the INDEPENDENT
  backup key. Without the key copy, a lost device would make restore
  impossible (CLAUDE.md key-protection rules).
- One-way cloud copy: upload-only to a Supabase Storage bucket
  (BACKUP_CLOUD_BUCKET) using SUPABASE_URL + SUPABASE_ANON_KEY with an
  insert-only storage policy. The device never holds the service-role key and
  never gains read/delete on the bucket — device theft cannot reach history.
- Restore drill: decrypt -> pg_restore into a scratch database -> sanity
  checks. "A backup that was never restored is not a backup."

The backup key itself must be exported ONCE by the owner (CLI: backup export-key)
and kept offline — it is the recovery root.
"""

import datetime as dt
import io
import json
import logging
import os
import subprocess
import tarfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pharmaos_api.security import keystore

logger = logging.getLogger(__name__)

_NONCE_SIZE = 12
_BACKUP_CONTEXT = b"pharmaos.backup.v1"
BACKUP_SUFFIX = ".pharmaos-backup"


def _database_url() -> str:
    from pharmaos_api.config import get_settings

    return get_settings().database_url


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
    """Run a postgres client tool; raise with its stderr on failure."""
    result = subprocess.run(cmd, capture_output=True, check=False, **kwargs)  # type: ignore[call-overload]  # noqa: S603
    if result.returncode != 0:
        stderr_head = result.stderr.decode(errors="replace")[:500]
        raise RuntimeError(f"{cmd[0]} failed (rc={result.returncode}): {stderr_head}")
    return result  # type: ignore[no-any-return]


def _keys_bundle() -> bytes:
    """Emergency key copy (JWT pair + field-encryption key), as JSON bytes."""
    private_pem, public_pem = keystore.ensure_jwt_keypair()
    field_key = keystore.ensure_encryption_key()
    return json.dumps(
        {
            "jwt_private_key_pem": private_pem,
            "jwt_public_key_pem": public_pem,
            "encryption_key_hex": field_key.hex(),
        }
    ).encode("utf-8")


def create_backup(backup_dir: Path, *, database_url: str | None = None) -> Path:
    """Produce an encrypted backup file and return its path."""
    url = database_url or _database_url()
    backup_dir.mkdir(parents=True, exist_ok=True)

    dump = _run(["pg_dump", "--format=custom", "--dbname", url]).stdout

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:

        def _add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(dt.datetime.now(dt.UTC).timestamp())
            tar.addfile(info, io.BytesIO(data))

        _add("db.dump", dump)
        _add("keys.json", _keys_bundle())
        _add(
            "meta.json",
            json.dumps(
                {
                    "created_at": dt.datetime.now(dt.UTC).isoformat(),
                    "format": "pharmaos.backup.v1",
                }
            ).encode("utf-8"),
        )

    nonce = os.urandom(_NONCE_SIZE)
    blob = nonce + AESGCM(keystore.ensure_backup_key()).encrypt(
        nonce, tar_buf.getvalue(), _BACKUP_CONTEXT
    )

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = backup_dir / f"pharmaos_{stamp}{BACKUP_SUFFIX}"
    out_path.write_bytes(blob)
    os.chmod(out_path, 0o600)
    logger.info("Backup created: %s (%d bytes)", out_path, len(blob))
    return out_path


def decrypt_backup(backup_file: Path) -> dict[str, bytes]:
    """Decrypt a backup file and return its members {name: bytes}."""
    blob = backup_file.read_bytes()
    nonce, ciphertext = blob[:_NONCE_SIZE], blob[_NONCE_SIZE:]
    tar_bytes = AESGCM(keystore.ensure_backup_key()).decrypt(nonce, ciphertext, _BACKUP_CONTEXT)
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tar:
        for member in tar.getmembers():
            extracted = tar.extractfile(member)
            if extracted is not None:
                members[member.name] = extracted.read()
    return members


def restore_drill(backup_file: Path, *, drill_database_url: str) -> dict[str, int]:
    """Restore into a DISPOSABLE database and return table counts (sanity check).

    Never points at the live database — the caller supplies a scratch DB URL.
    """
    members = decrypt_backup(backup_file)
    if "db.dump" not in members or "keys.json" not in members:
        raise RuntimeError("backup is missing required members")

    _run(
        ["pg_restore", "--clean", "--if-exists", "--no-owner", "--dbname", drill_database_url],
        input=members["db.dump"],
    )

    out = _run(
        [
            "psql",
            drill_database_url,
            "-tA",
            "-c",
            "SELECT COUNT(*) FROM users; SELECT COUNT(*) FROM permissions;",
        ]
    ).stdout
    users_count, permissions_count = (int(line) for line in out.decode().strip().splitlines())
    json.loads(members["keys.json"])  # keys bundle must parse — restore depends on it
    return {"users": users_count, "permissions": permissions_count}


def upload_to_cloud(backup_file: Path) -> bool:
    """One-way encrypted copy to Supabase Storage. Returns False (with a warning)
    when the cloud is not configured yet — backup remains local-only."""
    supabase_url = os.environ.get("SUPABASE_URL", "")
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
    bucket = os.environ.get("BACKUP_CLOUD_BUCKET", "")
    if not (supabase_url and anon_key and bucket):
        logger.warning("Cloud backup not configured (SUPABASE_URL/ANON_KEY/BACKUP_CLOUD_BUCKET).")
        return False

    host = urlparse(supabase_url).netloc
    if not host:
        raise RuntimeError("SUPABASE_URL is not a valid URL")

    object_path = f"{backup_file.name}"
    endpoint = f"{supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{object_path}"
    response = httpx.post(
        endpoint,
        content=backup_file.read_bytes(),
        headers={
            "Authorization": f"Bearer {anon_key}",
            "apikey": anon_key,
            "Content-Type": "application/octet-stream",
            "x-upsert": "false",  # one-way: never overwrite history
        },
        timeout=120,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"cloud upload failed: HTTP {response.status_code}")
    logger.info("Backup uploaded to cloud bucket '%s' as '%s'.", bucket, object_path)
    return True
