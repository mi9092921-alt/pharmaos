"""CLI entry point — init / issue / list / verify / export / import."""

import argparse
import base64
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from pharmaos_license_cli import keys as keys_mod
from pharmaos_license_cli import ledger as ledger_mod
from pharmaos_license_cli.canonical import canonical_json_bytes
from pharmaos_license_cli.payload import (
    CONTAINER_FORMAT,
    PRESETS,
    LicensePayloadV1,
    next_license_id,
)


def _load_private_key(path: Path) -> tuple[str, Ed25519PrivateKey]:
    passphrase = keys_mod.prompt_passphrase(confirm=False)
    kid, seed = keys_mod.load_key_file(path, passphrase=passphrase)
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    return kid, private_key


def _public_pem(private_key: Ed25519PrivateKey) -> str:
    return (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def cmd_init(args: argparse.Namespace) -> int:
    key_path = Path(args.key_file)
    if key_path.is_file() and not args.force:
        print(f"refusing to overwrite existing key file: {key_path} (use --force)")
        return 1
    kid = keys_mod.generate_kid()
    seed = secrets_seed()
    passphrase = keys_mod.prompt_passphrase(confirm=True)
    keys_mod.save_key_file(key_path, kid=kid, private_seed=seed, passphrase=passphrase)
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    print()
    print("Issuer keypair generated.")
    print(f"  kid:       {kid}")
    print(f"  key file:  {key_path}  (passphrase-encrypted, NEVER commit)")
    print()
    print("Bake this public key into the application")
    print("(apps/api/src/pharmaos_api/licensing/vendor_key.py):")
    print()
    print(_public_pem(private_key))
    return 0


def secrets_seed() -> bytes:
    import os

    return os.urandom(32)


def cmd_issue(args: argparse.Namespace) -> int:
    key_path = Path(args.key_file)
    kid, private_key = _load_private_key(key_path)
    issued_at = datetime.now(UTC)

    if args.preset is not None and args.months is not None:
        print("use either --preset or --months, not both")
        return 2
    if args.preset is not None:
        if args.preset == "trial":
            valid_until = issued_at + timedelta(days=14)
            kind = "trial"
        elif args.preset == "emergency":
            valid_until = issued_at + timedelta(days=7)
            kind = "emergency"
        elif args.preset == "monthly":
            valid_until = issued_at + timedelta(days=31)
            kind = "subscription"
        elif args.preset == "annual":
            valid_until = issued_at + timedelta(days=365)
            kind = "subscription"
        else:
            print(f"unknown preset: {args.preset} (expected one of {sorted(PRESETS)})")
            return 2
    elif args.months is not None:
        if args.months <= 0:
            print("--months must be positive")
            return 2
        valid_until = issued_at + timedelta(days=30 * args.months)
        kind = "subscription"
    else:
        print("one of --preset / --months is required")
        return 2

    entries = ledger_mod.read_entries(Path(args.ledger))
    license_id = next_license_id(issued_at, ledger_mod.issued_numbers(entries))
    features = [f.strip() for f in args.features.split(",")] if args.features else []

    payload = LicensePayloadV1(
        schema_version=1,
        license_id=license_id,
        customer=args.customer,
        hwid=args.hwid,
        issued_at=issued_at,
        valid_until=valid_until,
        kind=kind,  # type: ignore[arg-type]
        features=features,
    )
    canonical = canonical_json_bytes(payload.to_canonical_dict())
    signature = private_key.sign(canonical)
    document = {
        "format": CONTAINER_FORMAT,
        "kid": kid,
        "payload": payload.to_canonical_dict(),
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    out_path = Path(args.out) if args.out else Path(f"{license_id}.license")
    out_path.write_bytes(canonical_json_bytes(document))

    ledger_mod.append_entry(
        Path(args.ledger),
        {
            "license_id": license_id,
            "customer": args.customer,
            "hwid": args.hwid,
            "kind": kind,
            "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
            "valid_until": valid_until.isoformat().replace("+00:00", "Z"),
            "features": features,
            "file": str(out_path),
            "kid": kid,
            "created_at": issued_at.isoformat().replace("+00:00", "Z"),
        },
    )
    print(f"issued {license_id} for {args.customer} ({args.hwid})")
    print(f"  valid until : {valid_until.isoformat().replace('+00:00', 'Z')}")
    print(f"  file        : {out_path}")
    print(f"  ledger      : {args.ledger}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    entries = ledger_mod.read_entries(Path(args.ledger))
    if args.expiring_soon is not None:
        entries = ledger_mod.expiring_soon(entries, args.expiring_soon)
    if not entries:
        print("no ledger entries")
        return 0
    for entry in entries:
        print(
            f"{entry.get('license_id')}  {str(entry.get('valid_until'))[:10]}  "
            f"{entry.get('kind'):<12} {entry.get('customer')}"
        )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    kid, private_key = _load_private_key(Path(args.key_file))
    public_key = private_key.public_key()
    from pharmaos_license_cli.canonical import loads_strict

    document = loads_strict(Path(args.file).read_bytes())
    payload = LicensePayloadV1.model_validate(document["payload"])
    signature = base64.b64decode(document["signature"], validate=True)
    public_key.verify(signature, canonical_json_bytes(payload.to_canonical_dict()))
    print(f"OK: signature valid (kid={kid}) for {payload.license_id} — {payload.customer}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    src = Path(args.key_file)
    dst = Path(args.out)
    dst.write_bytes(src.read_bytes())
    try:
        dst.chmod(0o600)
    except OSError:
        pass
    print(f"exported encrypted key file to {dst}")
    print("Store this OFFLINE (USB + paper passphrase). It never touches a customer device.")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    dst = Path(args.key_file)
    if dst.is_file() and not args.force:
        print(f"refusing to overwrite existing key file: {dst} (use --force)")
        return 1
    dst.write_bytes(Path(args.file).read_bytes())
    try:
        dst.chmod(0o600)
    except OSError:
        pass
    print(f"imported key file to {dst}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pharmaos-license",
        description="PharmaOS vendor-side license issuing tool (owner machine only).",
    )
    parser.add_argument(
        "--key-file",
        default=str(keys_mod.DEFAULT_KEY_FILE),
        help=f"encrypted issuer key file (default {keys_mod.DEFAULT_KEY_FILE})",
    )
    parser.add_argument(
        "--ledger",
        default=str(keys_mod.DEFAULT_LEDGER_FILE),
        help=f"issuance ledger JSONL (default {keys_mod.DEFAULT_LEDGER_FILE})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="generate the issuer keypair (once, ever)")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    issue = sub.add_parser("issue", help="issue a license file for one device")
    issue.add_argument("--customer", required=True)
    issue.add_argument("--hwid", required=True, help="PHAR-XXXX-XXXX-XXXX from the activation screen")
    issue.add_argument(
        "--preset", choices=sorted(PRESETS), help="trial=14d, monthly=31d, annual=365d, emergency=7d"
    )
    issue.add_argument("--months", type=int, help="custom duration in 30-day months")
    issue.add_argument("--features", default="", help="comma-separated (v1: stored only)")
    issue.add_argument("--out", help="output file (default <license_id>.license)")
    issue.set_defaults(func=cmd_issue)

    listing = sub.add_parser("list", help="ledger entries (your CRM)")
    listing.add_argument("--expiring-soon", type=int, metavar="DAYS")
    listing.set_defaults(func=cmd_list)

    verify = sub.add_parser("verify", help="verify a license file before sending")
    verify.add_argument("--file", required=True)
    verify.set_defaults(func=cmd_verify)

    export = sub.add_parser("export", help="export the encrypted key file for offline backup")
    export.add_argument("--out", required=True)
    export.set_defaults(func=cmd_export)

    importer = sub.add_parser("import", help="import an exported key file onto this machine")
    importer.add_argument("--file", required=True)
    importer.add_argument("--force", action="store_true")
    importer.set_defaults(func=cmd_import)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except keys_mod.KeyFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
