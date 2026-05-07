from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

ENCRYPTED_SECRET_PREFIX = "enc:v1:"


class SecretDecryptionError(RuntimeError):
    pass


def is_encrypted_secret(value: str | None) -> bool:
    return bool(value and value.startswith(ENCRYPTED_SECRET_PREFIX))


def _fernet_key() -> bytes:
    configured = getattr(settings, "LLM_PROVIDER_API_KEY_ENCRYPTION_KEY", "").strip()
    if configured:
        try:
            Fernet(configured.encode("utf-8"))
            return configured.encode("utf-8")
        except (ValueError, TypeError):
            key_material = configured
    else:
        key_material = settings.SECRET_KEY

    digest = hashlib.sha256(key_material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_fernet_key())


def encrypt_secret(value: str | None) -> str | None:
    if value in (None, ""):
        return value
    if is_encrypted_secret(value):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_SECRET_PREFIX}{token}"


def decrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    if not is_encrypted_secret(value):
        # Backward compatibility for existing plaintext rows. The next save encrypts it.
        return value
    token = value.removeprefix(ENCRYPTED_SECRET_PREFIX)
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptionError(
            "Stored provider API key could not be decrypted."
        ) from exc
