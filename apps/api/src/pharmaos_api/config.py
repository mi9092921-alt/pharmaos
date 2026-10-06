"""Application settings.

.env carries ONLY non-secret configuration (CLAUDE.md secrets policy).
Critical secrets (JWT keys, ENCRYPTION_KEY, DB_PASSWORD, backup key) come
from the OS keystore via pharmaos_api.security.keystore — never from .env.

Runtime path contract (installer M1, decisions 3/4):
- PHARMAOS_ENV_FILE / PHARMAOS_DATA_DIR make every path independent of the
  CWD (Task Scheduler, Electron, CLI, PowerShell all resolve identically)
  via a custom dotenv settings source — the static `env_file=` in
  model_config is a CWD-relative constant and cannot do this.
- PRODUCTION is fail-closed: the environment name is read from the process
  environment ONLY (the device launcher sets it before any child starts),
  the data dir and env file are FIXED (C:\\ProgramData\\PharmaOS whatever
  the user's shell exports), and an explicit DATABASE_URL is REJECTED —
  the device runtime builds its URL from the keystore DB_PASSWORD.
"""

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Dev/CI fallback when nothing else is configured (docker-compose parity).
_DEV_DATABASE_URL = "postgresql://pharmaos:pharmaos@localhost:5432/pharmaos"
_PRODUCTION_DATA_DIR_NAME = "PharmaOS"


class ConfigError(RuntimeError):
    """Runtime configuration is inconsistent — fail fast with a clear cause."""


def process_env_name() -> str:
    """The environment name, from the process environment only."""
    return os.environ.get("PHARMAOS_ENV", "development")


def is_production_process() -> bool:
    return process_env_name() == "production"


def default_data_dir() -> Path:
    """Device data root (pgdata/backups/logs/.env live under it).

    Production: FIXED %ProgramData%\\PharmaOS — a user's shell cannot
    redirect it. Dev/CI: PHARMAOS_DATA_DIR or the CWD.
    """
    if is_production_process():
        # PROGRAMDATA is the canonical name; Windows env lookup is
        # case-insensitive so this resolves on every device.
        program_data = os.environ.get("PROGRAMDATA") or "C:\\ProgramData"
        return Path(program_data) / _PRODUCTION_DATA_DIR_NAME
    raw = os.environ.get("PHARMAOS_DATA_DIR")
    return Path(raw).expanduser() if raw else Path.cwd()


def runtime_env_file() -> Path:
    """The .env the settings loader reads. Production: the fixed device path
    (env-provided PHARMAOS_ENV_FILE is ignored — decision 3)."""
    if is_production_process():
        return default_data_dir() / ".env"
    raw = os.environ.get("PHARMAOS_ENV_FILE")
    return Path(raw).expanduser() if raw else Path(".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")

    # Environment: development | test | production
    pharmaos_env: str = "development"

    # Explicit DATABASE_URL override — dev/CI/docker only. Production REJECTS
    # it (fail-closed, decision 3); the device builds its URL from the
    # keystore DB_PASSWORD instead (decision 1 — the password is never in .env).
    database_url: str = ""

    # URL components used when building from the keystore (device runtime).
    db_host: str = "127.0.0.1"
    db_port: int = 5433
    db_name: str = "pharmaos"
    db_user: str = "pharmaos"

    # Bundled PostgreSQL binaries directory (the installer points this at
    # <install>\\resources\\pg\\bin). Empty = resolve from PATH (dev/CI).
    pg_bin_dir: str = ""

    # The pharmacy's LOCAL time zone (P3-M7 root fix). Every local-day semantic
    # in the product (Z-report, daily invoice sequences, report date windows,
    # CURRENT_DATE) resolves through the DB session's TimeZone — a docker PG
    # defaults to UTC, so a sale at 00:30 local would land on the WRONG day and
    # "today" reports would silently go empty. The session timezone is forced
    # to this IANA zone on every connection (db.py); it must match the device's
    # clock (Africa/Cairo = the primary market).
    device_timezone: str = "Africa/Cairo"

    # Bind address — CLAUDE.md security: local API listens on 127.0.0.1 ONLY.
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # JWT (CLAUDE.md mandatory settings)
    jwt_algorithm: str = "RS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_hours: int = 7 * 24

    # Password policy (CLAUDE.md)
    min_password_length: int = 8
    require_uppercase: bool = True
    require_number: bool = True
    require_special: bool = True
    max_login_attempts: int = 5
    lockout_minutes: int = 15

    # Login rate limit (CLAUDE.md: login 5/minute)
    login_rate_limit_per_minute: int = 5

    # Cookies
    cookie_secure: bool = False  # True in cloud (HTTPS); local device is localhost HTTP
    access_cookie_name: str = "pharmaos_access"
    refresh_cookie_name: str = "pharmaos_refresh"
    csrf_cookie_name: str = "pharmaos_csrf"

    # Country/currency defaults (Egypt per CLAUDE.md; configurable per branch)
    country_code: str = "EG"
    default_currency: str = "EGP"

    # Receipt printer (P1-M9) — device-local, non-secret. Network ESC/POS
    # (JetDirect port 9100); the Electron USB transport is a later hardware item.
    printer_host: str | None = None
    printer_port: int = 9100
    printer_timeout_seconds: float = 5.0

    @property
    def resolved_database_url(self) -> str:
        """The URL the runtime actually connects with.

        Precedence: an explicit DATABASE_URL wins in dev/CI/docker; on a
        production device it is REJECTED (a user shell must not be able to
        redirect the runtime to another database). With no explicit URL the
        URL is built from the keystore DB_PASSWORD; in production a missing
        password means first-run never completed — fail loudly. Dev keeps the
        docker-compose default so `docker compose up` keeps working.
        """
        if self.database_url:
            if self.is_production:
                raise ConfigError(
                    "DATABASE_URL must not be set on a production device — the "
                    "runtime builds it from the keystore DB_PASSWORD (fail-closed)."
                )
            return self.database_url

        from pharmaos_api.security import keystore  # local: keystore imports config

        password = keystore.get_db_password()
        if password:
            return (
                f"postgresql://{self.db_user}:{quote(password, safe='')}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        if self.is_production:
            raise ConfigError(
                "DB_PASSWORD is missing from the keystore — first-run setup has "
                "not completed on this device (open PharmaOS to run setup)."
            )
        return _DEV_DATABASE_URL

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy async URL (asyncpg driver)."""
        url = self.resolved_database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def is_production(self) -> bool:
        return self.pharmaos_env == "production"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The dotenv source reads the RUNTIME-resolved env file (decision 4) —
        # a static model_config env_file would pin the CWD-relative "./.env".
        return (
            init_settings,
            env_settings,
            DotEnvSettingsSource(
                settings_cls, env_file=runtime_env_file(), env_file_encoding="utf-8"
            ),
            file_secret_settings,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
