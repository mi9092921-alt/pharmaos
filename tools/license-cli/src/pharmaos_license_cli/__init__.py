"""PharmaOS vendor-side license issuing tool.

OWNER-SIDE ONLY — never shipped to customers (P4 §2). The private signing key
lives in a passphrase-encrypted file OUTSIDE this repository; the public key is
baked into the application (pharmaos_api.licensing.vendor_key).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
