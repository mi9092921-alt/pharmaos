"""MAC-authenticated external clock stores (P4 §1/§3 — LOCK-2).

Two stores on Windows (ProgramData file + HKCU registry), one POSIX fallback
file for dev/CI. Every record is stored as {"record": {...}, "mac": hex} where

    ext_key = HKDF-SHA256(ikm=LICENSE_CLOCK_HMAC_KEY, salt=b"",
                          info=b"pharmaos.clock.ext.v1", L=32)
    mac     = HMAC-SHA256(ext_key, canonical_json_bytes(record))

A missing store is ABSENT (initialization/resync semantics — P4 §3); a present
store with a failing MAC is TAMPER (E-LIC-006). The effective boundary is
keystore read access — declared in the threat model (§0).
"""

import abc
import hashlib
import hmac
import os
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from pharmaos_api.errors import ErrorCode
from pharmaos_api.licensing.canonical import (
    DuplicateKeyError,
    canonical_json_bytes,
    loads_strict,
)
from pharmaos_api.licensing.errors import LicensingError

EXT_INFO = b"pharmaos.clock.ext.v1"
_ENVELOPE_KEYS = frozenset({"record", "mac"})


def derive_ext_key(clock_key: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=EXT_INFO).derive(clock_key)


def mac_record(ext_key: bytes, record: dict[str, Any]) -> str:
    return hmac.new(ext_key, canonical_json_bytes(record), hashlib.sha256).hexdigest()


def record_mac_matches(ext_key: bytes, record: dict[str, Any], mac: str) -> bool:
    return hmac.compare_digest(mac_record(ext_key, record), mac)


class ExternalStoreProvider(abc.ABC):
    """Raw byte read/write over one store location. Implementations raise
    OSError on IO failure; absence is returned as None (never an error)."""

    name: str

    @abc.abstractmethod
    def read_raw(self) -> bytes | None:
        """Stored bytes, or None when the store does not exist yet."""

    @abc.abstractmethod
    def write_raw(self, data: bytes) -> None:
        """Persist the envelope bytes (creating parent dirs as needed)."""


def read_store(provider: ExternalStoreProvider, ext_key: bytes) -> dict[str, Any] | None:
    """Authenticated read — None when absent; E-LIC-006 on any parse/MAC
    failure (a present store that does not authenticate is tamper, not noise)."""
    raw = provider.read_raw()
    if raw is None:
        return None
    try:
        envelope = loads_strict(raw)
    except DuplicateKeyError as exc:
        raise LicensingError(ErrorCode.LICENSE_TAMPER_DETECTED, f"{provider.name}:corrupt") from exc
    except ValueError as exc:
        raise LicensingError(ErrorCode.LICENSE_TAMPER_DETECTED, f"{provider.name}:corrupt") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != _ENVELOPE_KEYS
        or not isinstance(envelope["record"], dict)
        or not isinstance(envelope["mac"], str)
    ):
        raise LicensingError(ErrorCode.LICENSE_TAMPER_DETECTED, f"{provider.name}:corrupt")
    if not record_mac_matches(ext_key, envelope["record"], envelope["mac"]):
        raise LicensingError(ErrorCode.LICENSE_TAMPER_DETECTED, f"{provider.name}:mac")
    return envelope["record"]


def write_store(provider: ExternalStoreProvider, ext_key: bytes, record: dict[str, Any]) -> None:
    envelope = {"record": record, "mac": mac_record(ext_key, record)}
    provider.write_raw(canonical_json_bytes(envelope))


class WindowsFileStore(ExternalStoreProvider):
    """%PROGRAMDATA%\\PharmaOS\\license-store.json"""

    def __init__(self, base_dir: Path | None = None) -> None:
        base = base_dir or Path(
            os.environ.get("PROGRAMDATA", str(Path.home() / "AppData" / "Local"))
        )
        self._path = Path(base) / "PharmaOS" / "license-store.json"
        self.name = "programdata"

    def read_raw(self) -> bytes | None:
        try:
            return self._path.read_bytes()
        except FileNotFoundError:
            return None

    def write_raw(self, data: bytes) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(data)


class WindowsRegistryStore(ExternalStoreProvider):
    """HKCU\\Software\\PharmaOS\\Clock → value `store` (REG_SZ JSON envelope)."""

    _SUBKEY = r"Software\PharmaOS\Clock"
    _VALUE = "store"

    def __init__(self) -> None:
        self.name = "registry"

    def read_raw(self) -> bytes | None:
        if sys.platform != "win32":
            return None
        try:
            import winreg

            hkey = winreg.HKEY_CURRENT_USER
            open_key = winreg.OpenKey
            query_value = winreg.QueryValueEx
            with open_key(hkey, self._SUBKEY) as key:
                value, _ = query_value(key, self._VALUE)
                return str(value).encode("utf-8")
        except OSError:
            return None

    def write_raw(self, data: bytes) -> None:
        if sys.platform != "win32":
            return  # this store only exists on Windows; POSIX uses its own provider
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._SUBKEY) as key:
            winreg.SetValueEx(key, self._VALUE, 0, winreg.REG_SZ, data.decode("utf-8"))


class PosixStateStore(ExternalStoreProvider):
    """$XDG_STATE_HOME/pharmaos/license-store.json — dev/CI shim."""

    def __init__(self, path: Path | None = None) -> None:
        base = path or Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        self._path = Path(base) / "pharmaos" / "license-store.json"
        self.name = "statefile"

    def read_raw(self) -> bytes | None:
        try:
            return self._path.read_bytes()
        except FileNotFoundError:
            return None

    def write_raw(self, data: bytes) -> None:
        self._path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._path.write_bytes(data)


def default_providers() -> list[ExternalStoreProvider]:
    """The two Windows stores per P4 §3; one POSIX shim elsewhere."""
    if sys.platform == "win32":
        return [WindowsFileStore(), WindowsRegistryStore()]
    return [PosixStateStore()]
