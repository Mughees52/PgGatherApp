"""Symmetric encryption for stored connection passwords.

A Fernet key is generated once and stored at ``storage/secret.key`` (chmod 600).
Passwords are encrypted at rest and only decrypted in-memory at collection time.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_fernet: Optional[Fernet] = None


def _load_or_create_key(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes()
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write with restrictive permissions from the start.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key(settings.secret_key_path))
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a password; returns ciphertext as a str safe to store in SQLite."""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt stored ciphertext. Raises ValueError if the token is invalid."""
    try:
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:  # pragma: no cover - defensive
        raise ValueError("Could not decrypt password (wrong or missing key)") from exc
