"""Runtime path + database-URL contract (installer decisions 1/3/4).

- PHARMAOS_ENV_FILE / PHARMAOS_DATA_DIR make paths CWD-independent outside
  production; production FIXES them (a user's shell cannot redirect).
- Explicit DATABASE_URL wins in dev/CI/docker and is REJECTED in production.
- With no explicit URL the runtime builds one from the keystore DB_PASSWORD;
  production without it fails loudly (first-run never completed).
"""

import os
from pathlib import Path

import pytest

from pharmaos_api import config
from pharmaos_api.config import ConfigError, Settings
from pharmaos_api.security import keystore


def test_env_file_honored_outside_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("device_timezone=Africa/Algiers\n", encoding="utf-8")
    monkeypatch.setenv("PHARMAOS_ENV", "development")
    monkeypatch.setenv("PHARMAOS_ENV_FILE", str(env_file))

    assert config.runtime_env_file() == env_file
    # The custom dotenv source actually feeds the settings (CWD-independent).
    settings = Settings(database_url="")
    assert settings.device_timezone == "Africa/Algiers"


def test_data_dir_override_outside_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PHARMAOS_ENV", "development")
    monkeypatch.setenv("PHARMAOS_DATA_DIR", str(tmp_path / "data"))
    assert config.default_data_dir() == tmp_path / "data"


def test_production_fixes_paths_ignoring_user_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision 3: in production the user's shell CANNOT redirect the runtime."""
    monkeypatch.setenv("PHARMAOS_ENV", "production")
    monkeypatch.setenv("PHARMAOS_DATA_DIR", "D:\\evil")
    monkeypatch.setenv("PHARMAOS_ENV_FILE", "D:\\evil\\.env")

    expected = Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "PharmaOS"
    assert config.default_data_dir() == expected
    assert config.runtime_env_file() == expected / ".env"


def test_production_rejects_explicit_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user shell must not be able to point the device at another database."""
    monkeypatch.setenv("PHARMAOS_ENV", "production")
    settings = Settings(database_url="postgresql://attacker@127.0.0.1:9999/evil")
    with pytest.raises(ConfigError, match="DATABASE_URL must not be set"):
        _ = settings.resolved_database_url


def test_device_url_built_from_keystore_db_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision 1: the password lives in the keystore and is URL-quoted."""
    monkeypatch.setattr(keystore, "get_db_password", lambda: "s3cret/+:x")
    monkeypatch.setenv("PHARMAOS_ENV", "production")
    settings = Settings(database_url="")  # explicit "" overrides the env value

    url = settings.resolved_database_url
    assert url == "postgresql://pharmaos:s3cret%2F%2B%3Ax@127.0.0.1:5433/pharmaos"
    assert settings.async_database_url == (
        "postgresql+asyncpg://pharmaos:s3cret%2F%2B%3Ax@127.0.0.1:5433/pharmaos"
    )


def test_production_without_db_password_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keystore, "get_db_password", lambda: None)
    monkeypatch.setenv("PHARMAOS_ENV", "production")
    settings = Settings(database_url="")
    with pytest.raises(ConfigError, match="first-run"):
        _ = settings.resolved_database_url


def test_dev_without_anything_falls_back_to_docker_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keystore, "get_db_password", lambda: None)
    monkeypatch.setenv("PHARMAOS_ENV", "development")
    settings = Settings(database_url="")
    assert (
        settings.resolved_database_url == "postgresql://pharmaos:pharmaos@localhost:5432/pharmaos"
    )


def test_pg_bin_dir_setting_exposed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PHARMAOS_ENV", "development")
    monkeypatch.setenv("PG_BIN_DIR", str(tmp_path / "pg" / "bin"))
    settings = Settings(database_url="")
    assert settings.pg_bin_dir == str(tmp_path / "pg" / "bin")
