"""Application settings.

.env carries ONLY non-secret configuration (CLAUDE.md secrets policy).
Critical secrets (JWT private key, ENCRYPTION_KEY) come from the OS keystore
via pharmaos_api.security.keystore — never from .env on production devices.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Environment: development | test | production
    pharmaos_env: str = "development"

    # Local database / cache (localhost only — the API must not listen externally)
    database_url: str = "postgresql://pharmaos:pharmaos@localhost:5432/pharmaos"
    redis_url: str = "redis://localhost:6379"

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
    def async_database_url(self) -> str:
        """SQLAlchemy async URL (asyncpg driver)."""
        url = self.database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def is_production(self) -> bool:
        return self.pharmaos_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
