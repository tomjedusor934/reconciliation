"""Symmetric encryption helpers used to protect SSO provider secrets at rest."""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """Return a cached Fernet instance derived from SSO_ENCRYPTION_KEY.

    A raw 32-byte url-safe base64 key is preferred. If a non-conforming string
    is supplied, we derive a deterministic Fernet key by SHA-256 hashing it,
    so existing deployments don't break — but a real Fernet.generate_key()
    value should be used in production.
    """
    raw = (settings.SSO_ENCRYPTION_KEY or "").strip()
    if not raw:
        raise RuntimeError(
            "SSO_ENCRYPTION_KEY is not configured. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`"
        )

    try:
        # Validate that this is a usable Fernet key as-is.
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode()).digest()
        derived = base64.urlsafe_b64encode(digest)
        return Fernet(derived)


def encrypt(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    token = _get_fernet().encrypt(value.encode("utf-8"))
    return token.decode("utf-8")


def decrypt(token: Optional[str]) -> Optional[str]:
    if token is None or token == "":
        return None
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None
