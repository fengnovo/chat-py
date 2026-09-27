"""Password hashing — mirrors packages/db/src/password.ts.

Uses hashlib.scrypt with Node.js crypto.scrypt default parameters
(N=16384, r=8, p=1, keylen=64) so hashes are interchangeable with the
TypeScript implementation: ``scrypt$<salt-hex>$<digest-hex>``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os

KEY_LENGTH = 64
# Node crypto.scrypt defaults.
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=KEY_LENGTH,
    )


async def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = await asyncio.to_thread(_scrypt, password, salt)
    return f"scrypt${salt.hex()}${derived.hex()}"


async def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    _, salt_hex, digest_hex = parts
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    if len(salt) == 0 or len(expected) != KEY_LENGTH:
        return False
    try:
        derived = await asyncio.to_thread(_scrypt, password, salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)
