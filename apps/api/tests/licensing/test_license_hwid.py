"""HWID failure matrix (P4 §6) — pure derivation + cache semantics."""

from pathlib import Path

import pytest

from pharmaos_api.licensing.hwid import (
    HWID_PATTERN,
    compute_hwid,
    hwid_from_sources,
)


def _ext_key() -> bytes:
    from pharmaos_api.licensing.external_stores import derive_ext_key

    return derive_ext_key(b"\x01" * 32)


def test_full_components() -> None:
    hwid = hwid_from_sources(" mb-serial ", "CPU-ID-9", "disk-1", "guid-1")
    assert HWID_PATTERN.match(hwid)
    # normalization: case/whitespace insensitive
    assert hwid == hwid_from_sources("MB-SERIAL", "cpu-id-9", "DISK-1", "GUID-1")


def test_empty_component_replaced_by_guid() -> None:
    with_guid = hwid_from_sources("", "CPU", "DISK", "GUID")
    with_value = hwid_from_sources("GUID", "CPU", "DISK", "GUID")
    assert with_guid == with_value


def test_all_empty_falls_back_to_guid_alone() -> None:
    assert hwid_from_sources("", "", "", "GUID") == hwid_from_sources(
        "GUID", "GUID", "GUID", "GUID"
    )


def test_powershell_failure_falls_back_to_guid() -> None:
    # the (baseboard="", ..., guid) path IS the total-failure fallback
    assert hwid_from_sources("", "", "", "GUID").startswith("PHAR-")


def test_deterministic_and_order_fixed() -> None:
    a = hwid_from_sources("B", "C", "D", "G")
    b = hwid_from_sources("D", "C", "B", "G")  # same parts, DIFFERENT order
    assert a != b  # component order is part of the identity
    assert a == hwid_from_sources("B", "C", "D", "G")


def test_hwid_differs_per_machine_guid() -> None:
    assert hwid_from_sources("", "", "", "GUID-1") != hwid_from_sources("", "", "", "GUID-2")


def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = tmp_path / "hwid.cache"
    key = _ext_key()
    first = compute_hwid(cache_path=cache, ext_key=key)
    second = compute_hwid(cache_path=cache, ext_key=key)
    assert first == second


def test_cache_is_not_trusted_on_tamper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "hwid.cache"
    key = _ext_key()
    compute_hwid(cache_path=cache, ext_key=key)
    # tamper with the cached value (keep the MAC) — must recompute, not trust
    hwid, mac = cache.read_text(encoding="utf-8").split("\n", 1)
    cache.write_text(f"PHAR-EVIL-TAM-PER-XXXX\n{mac}", encoding="utf-8")

    def _fake_collect() -> tuple[str, str, str, str]:
        return ("real", "real", "real", "guid")

    monkeypatch.setattr("pharmaos_api.licensing.hwid.collect_sources", _fake_collect)
    result = compute_hwid(cache_path=cache, ext_key=key)
    assert result == hwid_from_sources("real", "real", "real", "guid")


def test_cache_skipped_without_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "hwid.cache"
    cache.write_text("PHAR-AAAA-BBBB-CCCC-DDDD\nbadmac\n", encoding="utf-8")
    calls = {"n": 0}

    def _fake_collect() -> tuple[str, str, str, str]:
        calls["n"] += 1
        return ("a", "b", "c", "d")

    monkeypatch.setattr("pharmaos_api.licensing.hwid.collect_sources", _fake_collect)
    compute_hwid(cache_path=cache, ext_key=None)
    assert calls["n"] == 1  # no key ⇒ no cache trust, full recompute
