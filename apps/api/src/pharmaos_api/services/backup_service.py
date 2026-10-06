"""Encrypted database backup, safe cluster-level restore, one-way cloud copy.

Design (CLAUDE.md + installer decisions 9/10/11):
- pg_dump (custom format) + an EMERGENCY COPY of the keystore keys (JWT pair,
  field-encryption key, and the DB password) are bundled into a tar archive,
  encrypted as ONE AES-256-GCM blob with the INDEPENDENT backup key. Without
  the key copy a lost device would make restore impossible; with the DB
  password included, cross-device recovery is self-contained (DPAPI keys are
  not portable — the backup envelope is the transport).
- One-way cloud copy: upload-only to a Supabase Storage bucket
  (BACKUP_CLOUD_BUCKET) using SUPABASE_URL + SUPABASE_ANON_KEY with an
  insert-only storage policy. The device never holds the service-role key and
  never gains read/delete on the bucket — device theft cannot reach history.
- Restore drill: decrypt -> pg_restore into a scratch database -> sanity
  checks. "A backup that was never restored is not a backup."
- Device restore is CLUSTER-LEVEL and fail-safe (restore_to_cluster): a
  staging cluster is built and verified FIRST — the live cluster is only
  swapped in after verification passes, with pgdata.previous as the rollback
  point. A failure at any step leaves the running device untouched.

The backup key itself must be exported ONCE by the owner (CLI: backup
export-key) and kept offline — it is the recovery root. Restoring on a fresh
device requires `backup import-key` FIRST (never as a command-line argument);
restore never silently generates a key (decision 10).
"""

import datetime as dt
import io
import json
import logging
import os
import shutil
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlparse

import asyncpg
import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pharmaos_api import __version__, pg_bin
from pharmaos_api.security import keystore
from pharmaos_api.utils_async import run_coro_sync

logger = logging.getLogger(__name__)

_NONCE_SIZE = 12
_BACKUP_CONTEXT = b"pharmaos.backup.v1"
BACKUP_SUFFIX = ".pharmaos-backup"
BACKUP_FORMAT = "pharmaos.backup.v1"
# Newest schema this runtime understands (keep in lockstep with migrations —
# a backup from a NEWER device must be rejected, not half-restored).
SUPPORTED_SCHEMA_VERSION = "20260711003000"
# PG 17 per docs/versions.md — pg_restore/dump are major-locked.
EXPECTED_PG_MAJOR = 17
LIVE_PORT = 5433
STAGING_PORT = 55433
_READY_TIMEOUT_SECONDS = 30


class BackupError(RuntimeError):
    """Backup/restore failed (decryption, members, verification)."""


class BackupKeyMissingError(BackupError):
    """No backup key in the keystore — `backup import-key` has not run."""


class BackupKeyMismatchError(BackupError):
    """The stored backup key does not decrypt this backup."""


class BackupIncompatibleError(BackupError):
    """The backup is not compatible with this runtime (format/PG major/schema)."""


def _database_url() -> str:
    from pharmaos_api.config import get_settings

    return get_settings().resolved_database_url


def default_backup_dir() -> Path:
    """Device: <data dir>/backups. Dev/CI: BACKUP_PATH or ./backups."""
    from pharmaos_api.config import default_data_dir, is_production_process

    if is_production_process():
        return default_data_dir() / "backups"
    env = os.environ.get("BACKUP_PATH")
    return Path(env) if env else Path("./backups")


def _run(
    cmd: list[str], *, input: bytes | None = None, timeout: int = 600
) -> subprocess.CompletedProcess[bytes]:
    """Run a postgres client tool; raise with its stderr on failure."""
    result = subprocess.run(  # noqa: S603
        cmd, capture_output=True, check=False, input=input, timeout=timeout
    )
    if result.returncode != 0:
        stderr_head = result.stderr.decode(errors="replace")[:500]
        raise BackupError(f"{cmd[0]} failed (rc={result.returncode}): {stderr_head}")
    return result


def _keys_bundle() -> bytes:
    """Emergency key copy: JWT pair + field key + DB password (JSON bytes).

    The DB password travels inside the encrypted envelope so a restored
    cluster is reachable on a NEW device (the DPAPI store there is empty and
    non-portable). Absent in dev backups that never provisioned one."""
    private_pem, public_pem = keystore.ensure_jwt_keypair()
    field_key = keystore.ensure_encryption_key()
    return json.dumps(
        {
            "jwt_private_key_pem": private_pem,
            "jwt_public_key_pem": public_pem,
            "encryption_key_hex": field_key.hex(),
            "db_password": keystore.get_db_password(),
        }
    ).encode("utf-8")


def _server_facts(url: str) -> dict[str, object]:
    """{postgres_major, schema_version} of the server behind `url` (best
    effort — None when unreachable/pre-migration; the compatibility gate
    treats unknown as unverifiable)."""

    async def _q() -> dict[str, object]:
        facts: dict[str, object] = {"postgres_major": None, "schema_version": None}
        try:
            conn = await asyncpg.connect(url, timeout=10)
        except Exception:
            return facts
        try:
            num = await conn.fetchval("SELECT current_setting('server_version_num')::int")
            if num is not None:
                facts["postgres_major"] = int(num) // 10000
            try:
                version = await conn.fetchval("SELECT MAX(version) FROM _pharmaos_migrations")
                if version is not None:
                    # versions are stored as full file stems
                    # (YYYYMMDDHHMMSS_description) — compare on the numeric
                    # stamp prefix only.
                    facts["schema_version"] = str(version).split("_", 1)[0]
            except Exception:  # table missing on a pre-migration cluster
                logger.debug("no _pharmaos_migrations on the backed-up cluster")
        finally:
            await conn.close()
        return facts

    return run_coro_sync(_q())


def create_backup(backup_dir: Path, *, database_url: str | None = None) -> Path:
    """Produce an encrypted backup file and return its path."""
    url = database_url or _database_url()
    backup_dir.mkdir(parents=True, exist_ok=True)

    dump = _run([str(pg_bin.resolve("pg_dump")), "--format=custom", "--dbname", url]).stdout

    facts = _server_facts(url)
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
                    "format": BACKUP_FORMAT,
                    "app_version": __version__,
                    "schema_version": facts["schema_version"],
                    "postgres_major": facts["postgres_major"],
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
    """Decrypt a backup file and return its members {name: bytes}.

    NEVER generates a key (decision 10): a missing key is a loud error, and a
    wrong key explains the import-key path instead of failing cryptically."""
    key = keystore.get_backup_key()
    if key is None:
        raise BackupKeyMissingError(
            "no backup key in the keystore — import the offline recovery key "
            "first: pharmaos-api backup import-key"
        )
    blob = backup_file.read_bytes()
    nonce, ciphertext = blob[:_NONCE_SIZE], blob[_NONCE_SIZE:]
    try:
        tar_bytes = AESGCM(key).decrypt(nonce, ciphertext, _BACKUP_CONTEXT)
    except InvalidTag as exc:
        hint = (
            " The stored key was auto-generated on THIS device — if the backup "
            "came from another device, import ITS offline key first."
            if not keystore.backup_key_imported()
            else ""
        )
        raise BackupKeyMismatchError(
            "backup decryption failed — the stored backup key does not match " "this backup." + hint
        ) from exc
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tar:
        for member in tar.getmembers():
            extracted = tar.extractfile(member)
            if extracted is not None:
                members[member.name] = extracted.read()
    return members


def import_backup_key(key_hex: str) -> None:
    """Store the owner's offline recovery key (decision 10). The key is never
    accepted as a command-line argument — hidden prompt or stdin only."""
    cleaned = key_hex.strip().lower()
    if len(cleaned) != 64 or any(c not in "0123456789abcdef" for c in cleaned):
        raise ValueError("the backup key must be exactly 32 bytes (64 hex characters)")
    keystore.set_secret(keystore.BACKUP_KEY_NAME, cleaned)
    keystore.mark_backup_key_imported()
    logger.info("Offline backup key imported (marked as owner-provided).")


def _parse_meta(members: dict[str, bytes]) -> dict[str, object]:
    if "meta.json" not in members:
        raise BackupError("backup is missing meta.json")
    try:
        meta = json.loads(members["meta.json"])
    except json.JSONDecodeError as exc:
        raise BackupError("meta.json is not valid JSON — backup is corrupt") from exc
    if meta.get("format") != BACKUP_FORMAT:
        raise BackupIncompatibleError(
            f"unsupported backup format {meta.get('format')!r} — expected {BACKUP_FORMAT!r}"
        )
    return meta  # type: ignore[no-any-return]


def _check_compatibility(meta: dict[str, object], *, our_pg_major: int) -> None:
    """Reject, before touching the live cluster, anything this runtime cannot
    faithfully restore (decision 9): unknown PG major, or a schema NEWER than
    the one this runtime ships."""
    backup_major = meta.get("postgres_major")
    if backup_major is not None and int(str(backup_major)) != our_pg_major:
        raise BackupIncompatibleError(
            f"backup was taken on PostgreSQL {backup_major}; this runtime ships "
            f"PostgreSQL {our_pg_major} — restore on matching binaries instead"
        )
    schema = str(meta.get("schema_version") or "").split("_", 1)[0]
    if schema and schema > SUPPORTED_SCHEMA_VERSION:
        raise BackupIncompatibleError(
            f"backup schema {schema} is NEWER than this runtime supports "
            f"({SUPPORTED_SCHEMA_VERSION}) — upgrade PharmaOS first"
        )


def _parse_keys(
    members: dict[str, bytes], *, require_db_password: bool = True
) -> dict[str, object]:
    if "keys.json" not in members:
        raise BackupError("backup is missing keys.json — restore would be impossible")
    try:
        keys = json.loads(members["keys.json"])
    except json.JSONDecodeError as exc:
        raise BackupError("keys.json is not valid JSON — backup is corrupt") from exc
    if not keys.get("db_password"):
        if not require_db_password:
            return keys  # type: ignore[no-any-return]  # drill: creds come from its URL
        # Cross-device restore requires the bundled password; same-device may
        # still fall back to the live keystore (older backup, same DPAPI store).
        fallback = keystore.get_db_password()
        if not fallback:
            raise BackupIncompatibleError(
                "backup predates the DB-password bundle and this keystore has no "
                "DB_PASSWORD — re-take the backup on the source device first"
            )
        keys["db_password"] = fallback
    return keys  # type: ignore[no-any-return]


# --------------------------------------------------------------------------
# Cluster-level safe restore (decision 11)
# --------------------------------------------------------------------------

_LIVE_SECRETS = (
    keystore.JWT_PRIVATE_KEY_NAME,
    keystore.JWT_PUBLIC_KEY_NAME,
    keystore.ENCRYPTION_KEY_NAME,
    keystore.DB_PASSWORD_NAME,
)


def _dsn(user: str, password: str, port: int, db_name: str) -> str:
    return f"postgresql://{user}:{quote(password, safe='')}@127.0.0.1:{port}/{db_name}"


def _conf_file(cluster_dir: Path) -> Path:
    return cluster_dir / "postgresql.conf"


def _pin_cluster_config(cluster_dir: Path, *, port: int) -> None:
    """Pin the runtime contract on a freshly initdb'd cluster: localhost only,
    the requested port. Later lines win, so appending is sufficient."""
    with _conf_file(cluster_dir).open("a", encoding="utf-8") as fh:
        fh.write(f"\nlisten_addresses = '127.0.0.1'\nport = {port}\n")


def _set_conf_port(cluster_dir: Path, *, port: int) -> None:
    """Rewrite the pinned port (staging -> live promotion keeps the contract)."""
    conf = _conf_file(cluster_dir)
    lines = conf.read_text(encoding="utf-8").splitlines(keepends=True)
    conf.write_text(
        "".join(f"port = {port}\n" if line.startswith("port = ") else line for line in lines),
        encoding="utf-8",
    )


def _secure_delete(path: Path) -> None:
    """Best-effort overwrite+delete for transient secret files (NTFS journaling
    makes true secure deletion impossible; this raises the bar)."""
    try:
        size = path.stat().st_size
        with path.open("r+b") as fh:
            fh.write(os.urandom(size))
            fh.flush()
            os.fsync(fh.fileno())
        path.unlink()
    except OSError:
        path.unlink(missing_ok=True)


def _pg_ctl(pgdata: Path, *args: str) -> None:
    _run([str(pg_bin.resolve("pg_ctl")), "-D", str(pgdata), *args])


def _pg_ctl_start(pgdata: Path, logfile: Path) -> None:
    """pg_ctl start WITHOUT captured pipes.

    pg_ctl detaches postgres.exe; if we capture stdout/stderr, the detached
    server inherits those pipe handles and keeps them open forever —
    subprocess.communicate() then hangs even after pg_ctl exits (Windows
    handle inheritance). The -l logfile is the diagnostic channel instead."""
    result = subprocess.run(  # noqa: S603
        [
            str(pg_bin.resolve("pg_ctl")),
            "-D",
            str(pgdata),
            "start",
            "-l",
            str(logfile),
        ],
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise BackupError(f"pg_ctl start failed (rc={result.returncode}) — see {logfile}")


def _wait_ready(port: int, *, timeout: int = _READY_TIMEOUT_SECONDS) -> None:
    pg_isready = pg_bin.resolve("pg_isready")
    for _ in range(timeout * 2):
        result = subprocess.run(  # noqa: S603
            [str(pg_isready), "-h", "127.0.0.1", "-p", str(port)],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise BackupError(f"PostgreSQL on port {port} did not become ready in {timeout}s")


def _stop_cluster(pgdata: Path, *, tolerate_not_running: bool) -> None:
    if tolerate_not_running and not (pgdata / "PG_VERSION").is_file():
        # Destroyed/empty dir (disaster restore) or never-provisioned path -
        # pg_ctl would only say "not a database cluster directory".
        logger.info("no cluster at %s - nothing to stop", pgdata)
        return
    try:
        _pg_ctl(pgdata, "stop", "-m", "fast")
    except BackupError as exc:
        msg = str(exc)
        stale = (
            "No such process" in msg  # force-killed server left a stale pidfile
            or "not running" in msg
            or "does not exist" in msg
        )
        if tolerate_not_running and stale:
            (pgdata / "postmaster.pid").unlink(missing_ok=True)
            logger.info("cluster at %s was not running (stale pidfile removed)", pgdata)
            return
        raise


def _init_staging(
    staging_dir: Path, *, db_user: str, db_password: str, port: int = STAGING_PORT
) -> None:
    """initdb with the runtime contract (decision 12): SCRAM auth, builtin
    C.UTF-8 (deterministic Arabic collation, no OS locale dependency),
    UTF-8, superuser = the app role with the RESTORED password."""
    initdb = pg_bin.resolve("initdb")
    pwfile = staging_dir.parent / f".pg-pwfile-{uuid.uuid4().hex[:8]}"
    pwfile.write_text(db_password + "\n", encoding="utf-8")
    try:
        _run(
            [
                str(initdb),
                "-D",
                str(staging_dir),
                "-U",
                db_user,
                "-A",
                "scram-sha-256",
                "--pwfile",
                str(pwfile),
                "--encoding=UTF8",
                "--locale-provider=builtin",
                "--builtin-locale=C.UTF-8",
            ]
        )
    finally:
        _secure_delete(pwfile)
    _pin_cluster_config(staging_dir, port=port)


async def _verify_staging_async(
    *, port: int, db_user: str, db_password: str, db_name: str
) -> dict[str, object]:
    dsn = _dsn(db_user, db_password, port, db_name)
    conn = await asyncpg.connect(dsn)
    try:
        missing = [
            table
            for table in ("users", "permissions", "installation_state")
            if await conn.fetchval("SELECT to_regclass($1) IS NULL", f"public.{table}")
        ]
        if missing:
            raise BackupError(f"restored schema is missing core tables: {', '.join(missing)}")
        users = int(await conn.fetchval("SELECT COUNT(*) FROM users") or 0)
        permissions = int(await conn.fetchval("SELECT COUNT(*) FROM permissions") or 0)
        # Encrypted-field spot check (decision 11): a real ciphertext must
        # decrypt with the RESTORED field key + the exact column context.
        encrypted_checked = False
        row = await conn.fetchrow(
            "SELECT national_id_encrypted FROM customers "
            "WHERE national_id_encrypted IS NOT NULL LIMIT 1"
        )
        decrypted_sample: str | None = None
        if row is not None:
            from pharmaos_api.security.crypto import decrypt_field
            from pharmaos_api.services.customer_service import NATIONAL_ID_CONTEXT

            plaintext = decrypt_field(
                bytes(row["national_id_encrypted"]), context=NATIONAL_ID_CONTEXT
            )
            decrypted_sample = plaintext[:3] + "…"  # never log full PII
            encrypted_checked = True
    finally:
        await conn.close()
    return {
        "users": users,
        "permissions": permissions,
        "encrypted_field_verified": encrypted_checked,
        "encrypted_sample": decrypted_sample,
    }


def _verify_staging(
    *, port: int, db_user: str, db_password: str, db_name: str
) -> dict[str, object]:
    return run_coro_sync(
        _verify_staging_async(port=port, db_user=db_user, db_password=db_password, db_name=db_name)
    )


def _capture_live_secrets() -> dict[str, str | None]:
    return {name: keystore.get_secret(name) for name in _LIVE_SECRETS}


def _import_restored_secrets(keys: dict[str, object]) -> None:
    keystore.set_secret(keystore.JWT_PRIVATE_KEY_NAME, str(keys["jwt_private_key_pem"]))
    keystore.set_secret(keystore.JWT_PUBLIC_KEY_NAME, str(keys["jwt_public_key_pem"]))
    keystore.set_secret(keystore.ENCRYPTION_KEY_NAME, str(keys["encryption_key_hex"]).lower())
    keystore.set_db_password(str(keys["db_password"]))


def _restore_live_secrets(saved: dict[str, str | None]) -> None:
    for name, value in saved.items():
        if value is None:
            keystore.delete_secret(name)
        else:
            keystore.set_secret(name, value)


def recover_interrupted_swap(pgdata_dir: Path) -> bool:
    """A crash between the two swap renames leaves pgdata missing and
    pgdata.previous intact — put the live cluster back. Returns True when a
    recovery happened (called at restore start; M4 also calls it at boot)."""
    previous = pgdata_dir.parent / "pgdata.previous"
    if not pgdata_dir.exists() and previous.exists():
        os.rename(previous, pgdata_dir)
        logger.warning("recovered interrupted restore swap: pgdata.previous -> pgdata")
        return True
    return False


def restore_to_cluster(
    backup_file: Path,
    *,
    pgdata_dir: Path,
    live_port: int = LIVE_PORT,
    staging_port: int = STAGING_PORT,
    stop_api: Callable[[], None] | None = None,
    start_api: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Safe cluster-level restore (decision 11).

    Order: decrypt + compatibility gates (live untouched) -> staging cluster
    initdb'd with the runtime contract -> pg_restore -> verify (schema,
    counts, real encrypted-field decrypt) -> ONLY THEN stop the live cluster,
    keep pgdata.previous, swap, import the restored secrets, re-run migrate
    (seeds), restart. Any failure rolls the swap and the keystore back — the
    device ends exactly as it started.
    """
    from pharmaos_api.config import get_settings

    settings = get_settings()
    db_user, db_name = settings.db_user, settings.db_name
    pgdata_dir = pgdata_dir.resolve()
    parent = pgdata_dir.parent
    previous_dir = parent / "pgdata.previous"
    recover_interrupted_swap(pgdata_dir)

    # --- Phase 1: read + gate (the live cluster is NOT touched) -------------
    members = decrypt_backup(backup_file)
    meta = _parse_meta(members)
    our_major = pg_bin.version_major(pg_bin.resolve("initdb"))
    if our_major is not None:
        _check_compatibility(meta, our_pg_major=our_major)
    else:  # pragma: no cover - --version parse failure on bundled binaries
        logger.warning("could not determine bundled PG major — skipping major gate")
    if "db.dump" not in members:
        raise BackupError("backup is missing db.dump")
    keys = _parse_keys(members)
    db_password = str(keys["db_password"])

    # --- Phase 2: build + verify staging (live still serving) ---------------
    staging_dir = parent / f"restore-staging-{uuid.uuid4().hex[:8]}"
    swapped = False
    try:
        staging_dir.mkdir(parents=True)
        _init_staging(staging_dir, db_user=db_user, db_password=db_password)
        _pg_ctl_start(staging_dir, parent / "pharmaos-pg-staging.log")
        _wait_ready(staging_port)

        async def _create_db() -> None:
            conn = await asyncpg.connect(_dsn(db_user, db_password, staging_port, "postgres"))
            try:
                await conn.execute(f'CREATE DATABASE "{db_name}"')
            finally:
                await conn.close()

        run_coro_sync(_create_db())
        _run(
            [
                str(pg_bin.resolve("pg_restore")),
                "--no-owner",
                "--dbname",
                _dsn(db_user, db_password, staging_port, db_name),
            ],
            input=members["db.dump"],
        )
        verification = _verify_staging(
            port=staging_port, db_user=db_user, db_password=db_password, db_name=db_name
        )
        logger.info("staging verification passed: %s", verification)
        # The staging server MUST be down before its data dir is renamed —
        # a running server holds open handles (rename would fail on Windows)
        # and a copied-under-it data dir would corrupt the promotion.
        _stop_cluster(staging_dir, tolerate_not_running=True)

        # --- Phase 3: swap (the only moment the live cluster is down) -------
        if stop_api is not None:
            stop_api()
        _stop_cluster(pgdata_dir, tolerate_not_running=True)
        os.rename(pgdata_dir, previous_dir)
        _set_conf_port(staging_dir, port=live_port)
        os.rename(staging_dir, pgdata_dir)
        swapped = True
        _pg_ctl_start(pgdata_dir, parent / "pharmaos-pg.log")
        _wait_ready(live_port)

        # --- Phase 4: restored secrets + post-promotion verification --------
        saved_secrets = _capture_live_secrets()
        _import_restored_secrets(keys)
        try:
            from pharmaos_api.migrations_runner import run_migrations

            migrations = run_migrations(
                _dsn(db_user, db_password, live_port, db_name),
                seeds_dir=None,  # default seeds dir — same as the CLI
            )
            if start_api is not None:
                start_api()
        except Exception:
            # Roll the device back to the exact pre-restore state.
            logger.exception("post-promotion step failed — rolling the swap back")
            _stop_cluster(pgdata_dir, tolerate_not_running=True)
            os.rename(pgdata_dir, staging_dir)
            os.rename(previous_dir, pgdata_dir)
            _pg_ctl_start(pgdata_dir, parent / "pharmaos-pg.log")
            _wait_ready(live_port)
            _restore_live_secrets(saved_secrets)
            if start_api is not None:
                start_api()
            raise
        shutil.rmtree(previous_dir, ignore_errors=True)
        logger.info("restore promoted and verified — rollback point removed")
        return {"verification": verification, "migrations": migrations}
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if not swapped:
            logger.info("restore aborted before promotion — live cluster untouched")


def restore_drill(backup_file: Path, *, drill_database_url: str) -> dict[str, int]:
    """Restore into a DISPOSABLE database and return table counts (sanity check).

    Never points at the live database — the caller supplies a scratch DB URL.
    """
    members = decrypt_backup(backup_file)
    if "db.dump" not in members or "keys.json" not in members:
        raise BackupError("backup is missing required members")

    _run(
        [
            str(pg_bin.resolve("pg_restore")),
            "--clean",
            "--if-exists",
            "--no-owner",
            "--dbname",
            drill_database_url,
        ],
        input=members["db.dump"],
    )

    out = _run(
        [
            str(pg_bin.resolve("psql")),
            drill_database_url,
            "-tA",
            "-c",
            "SELECT COUNT(*) FROM users; SELECT COUNT(*) FROM permissions;",
        ]
    ).stdout
    users_count, permissions_count = (int(line) for line in out.decode().strip().splitlines())
    _parse_keys(members, require_db_password=False)  # keys bundle must parse
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
        raise BackupError("SUPABASE_URL is not a valid URL")

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
        raise BackupError(f"cloud upload failed: HTTP {response.status_code}")
    logger.info("Backup uploaded to cloud bucket '%s' as '%s'.", bucket, object_path)
    return True
