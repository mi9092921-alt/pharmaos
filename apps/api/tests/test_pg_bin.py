"""Central PostgreSQL binary resolver (installer decision 6)."""

import os
from pathlib import Path

import pytest

from pharmaos_api import pg_bin


def _exe(tool: str) -> str:
    return f"{tool}.exe" if os.name == "nt" else tool


class _FakeResult:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


def test_resolve_prefers_bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / _exe("pg_dump")
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(pg_bin, "configured_pg_bin_dir", lambda: tmp_path)
    assert pg_bin.resolve("pg_dump") == fake


def test_resolve_configured_dir_must_be_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundled bin dir missing a tool is an error — never a silent PATH
    fallback (the device bundle must be exactly what the runtime expects)."""
    monkeypatch.setattr(pg_bin, "configured_pg_bin_dir", lambda: tmp_path)
    monkeypatch.setattr(pg_bin.shutil, "which", lambda _t: "C:\\should-not-be-used")
    with pytest.raises(pg_bin.PgBinaryNotFoundError, match="incomplete"):
        pg_bin.resolve("pg_dump")


def test_resolve_falls_back_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / _exe("pg_ctl")
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(pg_bin, "configured_pg_bin_dir", lambda: None)
    monkeypatch.setattr(pg_bin.shutil, "which", lambda tool: str(tmp_path / _exe(tool)))
    assert pg_bin.resolve("pg_ctl") == fake


def test_resolve_missing_everywhere_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pg_bin, "configured_pg_bin_dir", lambda: None)
    monkeypatch.setattr(pg_bin.shutil, "which", lambda _t: None)
    with pytest.raises(pg_bin.PgBinaryNotFoundError, match="PG_BIN_DIR"):
        pg_bin.resolve("psql")


def test_resolve_rejects_unknown_tool() -> None:
    with pytest.raises(ValueError, match="unknown PostgreSQL tool"):
        pg_bin.resolve("not-a-pg-tool")


def test_version_major_parses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / _exe("pg_dump")
    fake.write_text("", encoding="utf-8")

    def _fake_run(*_a: object, **_k: object) -> _FakeResult:
        return _FakeResult("pg_dump (PostgreSQL) 17.2")

    monkeypatch.setattr(pg_bin.subprocess, "run", _fake_run)
    assert pg_bin.version_major(fake) == 17


def test_version_major_unparseable_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = tmp_path / _exe("pg_dump")
    fake.write_text("", encoding="utf-8")

    def _fake_run(*_a: object, **_k: object) -> _FakeResult:
        return _FakeResult("something unexpected")

    monkeypatch.setattr(pg_bin.subprocess, "run", _fake_run)
    assert pg_bin.version_major(fake) is None


def test_real_binary_on_path_if_available() -> None:
    """On CI (and dev machines with PG on PATH) the resolver finds the real
    client and its version parses; locally without PG it self-skips."""
    try:
        path = pg_bin.resolve("pg_dump")
    except pg_bin.PgBinaryNotFoundError:
        pytest.skip("no PostgreSQL client on PATH")
    assert pg_bin.version_major(path) is not None
