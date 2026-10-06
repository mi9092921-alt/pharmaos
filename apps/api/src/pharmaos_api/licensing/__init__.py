"""Offline licensing core (P4-M1) — docs/phase4-execution-plan-licensing.md.

Cryptography contracts (§1/§2): Ed25519-signed license files bound to a
hardware fingerprint, an HMAC-chained clock-event ledger (DB-enforced structure,
app-enforced authenticity), and MAC-authenticated external clock stores.

The module is deliberately UI/router-free: M2 wires the gate middleware, boot
job, and router on top of exactly these functions. Every function that touches
the database takes its session factory as an explicit parameter (P4 §4 — no
hidden dependency on DATABASE_URL / get_settings).
"""
