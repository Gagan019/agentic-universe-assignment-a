"""PII protection at rest.

* Phone numbers are matched via a keyed hash (HMAC-SHA256) -> deterministic, so events can be
  joined to leads, but not reversible without the key (a plain SHA-256 of a 10-digit number would
  be brute-forceable in seconds).
* All other PII fields (names, and the canonical phone itself) are encrypted with Fernet.
* Only the last 4 digits are stored in the clear, solely to render `phone_masked`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

KEY_ENV = "AU_PII_KEY"


def load_master_key(db_path: str) -> bytes:
    """Key from $AU_PII_KEY, else a 0600 key file next to the DB (created on first use)."""
    env = os.environ.get(KEY_ENV)
    if env:
        return env.encode()
    if db_path == ":memory:":
        return secrets.token_bytes(32)
    kp = Path(db_path + ".key")
    if not kp.exists():
        fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(base64.urlsafe_b64encode(secrets.token_bytes(32)))
    return kp.read_bytes().strip()


class Pii:
    def __init__(self, master: bytes):
        self._hash_key = hmac.new(master, b"au/phone-hash", hashlib.sha256).digest()
        enc = hmac.new(master, b"au/encrypt", hashlib.sha256).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(enc))

    def phone_hash(self, canonical_phone: str) -> str:
        return hmac.new(self._hash_key, canonical_phone.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, obj: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(obj, sort_keys=True).encode())

    def decrypt(self, token: bytes) -> dict:
        return json.loads(self._fernet.decrypt(token))
