"""Hardware fingerprint (P4 §6 — the HWID failure matrix).

Order-fixed components, each normalized (strip + upper): motherboard serial,
CPU ProcessorId, system-disk serial. A missing/empty component is replaced by
the MachineGuid value itself; total PowerShell failure falls back to the
MachineGuid alone (winreg). The cache is a best-effort optimization keyed by an
HMAC over the value — never trusted on mismatch, recomputed when absent.

"Stable across a Windows reinstall" is an expectation backed by the
hardware-first anchoring, NOT a guarantee — a changed fingerprint has the
documented re-activation path (same customer, new file).
"""

import base64
import hashlib
import hmac
import os
import re
import subprocess
import sys
from pathlib import Path

HWID_PATTERN = re.compile(r"^PHAR-[A-Z0-9]{4}(-[A-Z0-9]{4}){3}$")
_HWID_DOMAIN = b"pharmaos.hwid.v1\n"
_CACHE_DOMAIN = b"pharmaos.hwid.cache.v1\n"
_TIMEOUT_SECONDS = 5.0

# One PowerShell round-trip for all four sources, positional output (empty
# lines preserved — a null SerialNumber must not shift the component order).
_PS_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "(Get-CimInstance Win32_BaseBoard).SerialNumber;"
    "(Get-CimInstance Win32_Processor | Select-Object -First 1).ProcessorId;"
    "(Get-CimInstance Win32_DiskDrive | Where-Object {$_.Index -eq 0}).SerialNumber;"
    "(Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Cryptography').MachineGuid"
)


def normalize_component(raw: str | None) -> str:
    return (raw or "").strip().upper()


def hwid_from_sources(
    baseboard: str | None, processor: str | None, disk: str | None, machine_guid: str | None
) -> str:
    """Pure fingerprint derivation — the unit-tested core of the matrix."""
    guid = normalize_component(machine_guid)
    parts = [
        normalize_component(baseboard) or guid,
        normalize_component(processor) or guid,
        normalize_component(disk) or guid,
    ]
    joined = b"\x1f".join(part.encode("utf-8") for part in parts)
    digest = hashlib.sha256(_HWID_DOMAIN + joined).digest()
    encoded = base64.b32encode(digest[:10]).decode("ascii")  # 16 chars, A-Z2-7 ⊂ [A-Z0-9]
    return "PHAR-" + "-".join(encoded[index : index + 4] for index in range(0, 16, 4))


def collect_sources() -> tuple[str, str, str, str]:
    """(baseboard, processor, disk, machine_guid) — platform-dependent."""
    if sys.platform == "win32":
        try:
            return _collect_windows()
        except (OSError, subprocess.TimeoutExpired):
            return ("", "", "", _machine_guid_winreg())
    return _collect_posix()


def _collect_windows() -> tuple[str, str, str, str]:
    result = subprocess.run(  # noqa: S603  # fixed binary, fully controlled args
        [  # noqa: S607
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _PS_SCRIPT,
        ],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    lines = [line.strip() for line in result.stdout.splitlines()]
    lines += [""] * (4 - len(lines))
    baseboard, processor, disk, machine_guid = lines[0], lines[1], lines[2], lines[3]
    if not machine_guid:
        machine_guid = _machine_guid_winreg()
    return baseboard, processor, disk, machine_guid


def _machine_guid_winreg() -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        hkey = winreg.HKEY_LOCAL_MACHINE
        open_key = winreg.OpenKey
        query_value = winreg.QueryValueEx
        with open_key(hkey, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _ = query_value(key, "MachineGuid")
            return str(value)
    except OSError:
        return ""


def _collect_posix() -> tuple[str, str, str, str]:
    """Dev/CI shim — the machine-id plays the MachineGuid role; hardware
    serials are usually unreadable unprivileged, so the guid replaces them."""

    def _read(path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    guid = _read("/etc/machine-id") or _read("/var/lib/dbus/machine-id")
    return (_read("/sys/class/dmi/id/board_serial"), "", "", guid)


def compute_hwid(*, cache_path: Path | None = None, ext_key: bytes | None = None) -> str:
    """Full computation with the best-effort MAC'd cache. Without the clock key
    the cache is skipped entirely (nothing to authenticate it with)."""
    if cache_path is not None and ext_key is not None:
        cached = _read_hwid_cache(cache_path, ext_key)
        if cached is not None:
            return cached
    hwid = hwid_from_sources(*collect_sources())
    if cache_path is not None and ext_key is not None:
        _write_hwid_cache(cache_path, ext_key, hwid)
    return hwid


def _read_hwid_cache(cache_path: Path, ext_key: bytes) -> str | None:
    try:
        raw = cache_path.read_text(encoding="utf-8")
        hwid, mac = raw.split("\n", 1)
        expected = hmac.new(ext_key, _CACHE_DOMAIN + hwid.encode("utf-8"), hashlib.sha256)
        if not hmac.compare_digest(expected.hexdigest(), mac.strip()):
            return None
        return hwid
    except (OSError, ValueError):
        return None


def _write_hwid_cache(cache_path: Path, ext_key: bytes, hwid: str) -> None:
    try:
        mac = hmac.new(ext_key, _CACHE_DOMAIN + hwid.encode("utf-8"), hashlib.sha256).hexdigest()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(f"{hwid}\n{mac}\n", encoding="utf-8")
        os.chmod(cache_path, 0o600)
    except OSError:
        pass  # the cache is an optimization — a failed write never blocks boot
