from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

ENC_PREFIX = "enc:"
PBKDF2_ITERATIONS = 600_000
SALT_SIZE = 16


def _derive_key(master_password: str, salt: bytes) -> bytes:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        master_password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(dk)


def encrypt(plaintext: str, master_password: str) -> str:
    salt = os.urandom(SALT_SIZE)
    key = _derive_key(master_password, salt)
    fernet = Fernet(key)
    ciphertext = fernet.encrypt(plaintext.encode("utf-8"))
    return ENC_PREFIX + base64.urlsafe_b64encode(salt + ciphertext).decode("ascii")


def decrypt(encrypted_value: str, master_password: str) -> Optional[str]:
    if not encrypted_value.startswith(ENC_PREFIX):
        return encrypted_value

    payload_b64 = encrypted_value[len(ENC_PREFIX):]
    try:
        payload = base64.urlsafe_b64decode(payload_b64)
    except Exception:
        return None

    if len(payload) < SALT_SIZE:
        return None

    salt = payload[:SALT_SIZE]
    ciphertext = payload[SALT_SIZE:]

    key = _derive_key(master_password, salt)
    fernet = Fernet(key)

    try:
        plaintext = fernet.decrypt(ciphertext)
        return plaintext.decode("utf-8")
    except InvalidToken:
        return None


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def decrypt_config(config: dict, master_password: str) -> dict:
    encrypted_fields = ("access_token", "password", "key_import_passphrase")

    for section_key in ("source", "target"):
        section = config.get(section_key)
        if not isinstance(section, dict):
            continue
        for field in encrypted_fields:
            value = section.get(field, "")
            if is_encrypted(value):
                decrypted = decrypt(value, master_password)
                if decrypted is None:
                    raise ValueError(
                        f"Failed to decrypt {section_key}.{field}. Wrong master password?"
                    )
                section[field] = decrypted

    return config
