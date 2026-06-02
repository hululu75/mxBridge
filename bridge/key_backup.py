"""
Matrix Megolm key backup restoration via SSSS recovery key.

Flow:
  1. Decode recovery key (base58 → 32 raw bytes)
  2. GET m.secret_storage.default_key  → key_id
  3. GET m.secret_storage.key.{key_id} → key metadata (verify key)
  4. GET account_data m.megolm_backup.v1 → encrypted backup private key
  5. SSSS-decrypt → raw 32-byte Curve25519 backup private key
  6. GET room_keys/version              → backup version + algorithm check
  7. GET room_keys/keys                 → all encrypted sessions
  8. X25519 + HKDF + AES-CTR decrypt each session key
  9. Build key export file, import via client.import_keys()
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json
import logging
import os
import struct
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# ---------------------------------------------------------------------------
# Base58 / recovery key decoding
# ---------------------------------------------------------------------------

def _base58_decode(s: str) -> bytes:
    n = 0
    for c in s:
        idx = _BASE58_ALPHABET.find(c)
        if idx < 0:
            raise ValueError(f"Invalid base58 character: {c!r}")
        n = n * 58 + idx
    result = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + result


def _decode_recovery_key(key_str: str) -> bytes:
    """Decode Matrix recovery key → 32-byte SSSS master key.

    Format: base58(0x8B 0x01 <32 bytes> <1-byte parity>)
    Accepts keys formatted with spaces, dashes, or colons as separators.
    """
    # Keep only valid base58 characters — strips spaces, dashes, colons, quotes, etc.
    clean = "".join(c for c in key_str if c in _BASE58_ALPHABET)
    logger.debug("[key_backup] Recovery key clean length=%d first4=%r", len(clean), clean[:4])
    raw = _base58_decode(clean)
    if len(raw) != 35:
        raise ValueError(f"Recovery key wrong decoded length: {len(raw)} (expected 35)")
    if raw[0] != 0x8B or raw[1] != 0x01:
        raise ValueError(f"Recovery key bad prefix bytes: {raw[:2].hex()} (expected 8b01)")
    parity = 0
    for b in raw[:34]:
        parity ^= b
    if parity != raw[34]:
        raise ValueError("Recovery key parity check failed — key is incorrect")
    return raw[2:34]


def _derive_ssss_key_from_passphrase(passphrase: str, passphrase_info: dict) -> bytes:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    salt = base64.b64decode(passphrase_info["salt"])
    iterations = int(passphrase_info.get("iterations", 500000))
    bits = int(passphrase_info.get("bits", 256))
    kdf = PBKDF2HMAC(algorithm=hashes.SHA512(), length=bits // 8, salt=salt, iterations=iterations)
    return kdf.derive(passphrase.encode())


# ---------------------------------------------------------------------------
# SSSS encryption helpers (m.secret_storage.v1.aes-hmac-sha2)
# ---------------------------------------------------------------------------

def _derive_ssss_enc_keys(ssss_key: bytes, name: str) -> tuple[bytes, bytes]:
    """Returns (aes_key, hmac_key) derived via HKDF-SHA256.

    matrix-js-sdk uses deriveBits(512) to get 64 bytes at once, then splits:
      aes_key  = output[0:32]  (HKDF T(1))
      hmac_key = output[32:64] (HKDF T(2))
    The two keys are DIFFERENT because they come from different expansion rounds.
    """
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    derived = HKDF(
        algorithm=hashes.SHA256(), length=64,
        salt=bytes(32), info=name.encode()
    ).derive(ssss_key)
    return derived[:32], derived[32:]


def _ssss_decrypt(ssss_key: bytes, name: str, encrypted: dict) -> bytes:
    """AES-256-CTR decrypt a SSSS-encrypted secret, verifying HMAC-SHA256."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    aes_key, mac_key = _derive_ssss_enc_keys(ssss_key, name)
    iv = base64.b64decode(encrypted["iv"])
    ct = base64.b64decode(encrypted["ciphertext"])
    mac = base64.b64decode(encrypted["mac"])
    expected = _hmac.new(mac_key, ct, hashlib.sha256).digest()
    if not _hmac.compare_digest(mac, expected):
        raise ValueError("SSSS MAC mismatch — recovery key is wrong or data is corrupt")
    cipher = Cipher(algorithms.AES(aes_key), modes.CTR(iv))
    d = cipher.decryptor()
    return d.update(ct) + d.finalize()


def _verify_ssss_key(ssss_key: bytes, key_metadata: dict) -> bool:
    """Verify the SSSS key is correct using the metadata's iv/mac."""
    if not key_metadata.get("mac") or not key_metadata.get("iv"):
        return True  # no verification data present
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        iv = base64.b64decode(key_metadata["iv"])
        expected_mac_b64 = key_metadata["mac"]
        aes_key, mac_key = _derive_ssss_enc_keys(ssss_key, "")  # empty name for key check
        cipher = Cipher(algorithms.AES(aes_key), modes.CTR(iv))
        enc = cipher.encryptor()
        encrypted_zeros = enc.update(bytes(32)) + enc.finalize()
        computed_mac = base64.b64encode(
            _hmac.new(mac_key, encrypted_zeros, hashlib.sha256).digest()
        ).decode()
        return computed_mac == expected_mac_b64
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Megolm backup session decryption (m.megolm_backup.v1.curve25519-aes-sha2)
# ---------------------------------------------------------------------------

def _make_pk_decryption(priv_bytes: bytes):
    """Create an olm.pk.PkDecryption using a specific private key (not random)."""
    import olm.pk as pk_module
    # PkDecryption.__init__ calls URANDOM(private_key_length) and passes the result
    # directly to olm_pk_key_from_private as the private key. We intercept URANDOM
    # to inject our key.
    original_urandom = pk_module.URANDOM
    try:
        pk_module.URANDOM = lambda _n: priv_bytes
        return pk_module.PkDecryption()
    finally:
        pk_module.URANDOM = original_urandom


def _decrypt_backup_session(priv_bytes: bytes, session_data: dict) -> Optional[dict]:
    """Decrypt one backup session using libolm PkDecryption (same as matrix-js-sdk)."""
    try:
        import olm.pk
        dec = _make_pk_decryption(priv_bytes)
        plaintext = dec.decrypt(
            olm.pk.PkMessage(
                ephemeral_key=session_data["ephemeral"],
                mac=session_data["mac"],
                ciphertext=session_data["ciphertext"],
            )
        )
        return json.loads(plaintext)
    except Exception as e:
        logger.debug("[key_backup] Session decrypt failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Key export file creation (for nio's import_keys)
# ---------------------------------------------------------------------------

def _create_key_export_data(sessions: list, passphrase: str) -> str:
    """Create an encrypted Matrix key export file (the format nio understands)."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    salt = os.urandom(16)
    iv = os.urandom(16)
    iterations = 1_000  # temp file deleted immediately — no need for high iteration count

    derived = PBKDF2HMAC(
        algorithm=hashes.SHA512(), length=64, salt=salt, iterations=iterations
    ).derive(passphrase.encode())
    aes_key, hmac_key = derived[:32], derived[32:]

    plaintext = json.dumps(sessions).encode()
    enc = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).encryptor()
    ct = enc.update(plaintext) + enc.finalize()

    header = bytes([0x01]) + salt + iv + struct.pack(">I", iterations)
    mac = _hmac.new(hmac_key, header + ct, hashlib.sha256).digest()
    data = base64.b64encode(header + ct + mac).decode()
    lines = "\n".join(data[i:i + 76] for i in range(0, len(data), 76))
    return (
        "-----BEGIN MEGOLM SESSION DATA-----\n"
        + lines + "\n"
        + "-----END MEGOLM SESSION DATA-----\n"
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def restore_key_backup(
    client,
    homeserver: str,
    access_token: str,
    recovery_key: str,
) -> int:
    """Fetch all megolm sessions from server key backup and import into nio.

    Returns number of sessions imported, 0 on any failure.
    """
    import aiohttp

    hs = homeserver.rstrip("/")
    uid = client.user_id
    headers = {"Authorization": f"Bearer {access_token}"}

    async with aiohttp.ClientSession() as http:
        # ── Step 1: default SSSS key ID ─────────────────────────────────────
        async with http.get(
            f"{hs}/_matrix/client/v3/user/{uid}/account_data/m.secret_storage.default_key",
            headers=headers,
        ) as r:
            if r.status != 200:
                logger.warning("[key_backup] No SSSS default key (HTTP %d)", r.status)
                return 0
            key_id = (await r.json()).get("key", "")
        if not key_id:
            logger.warning("[key_backup] m.secret_storage.default_key has no 'key' field")
            return 0
        logger.info("[key_backup] SSSS key_id=%s", key_id)

        # ── Step 2: key metadata ─────────────────────────────────────────────
        async with http.get(
            f"{hs}/_matrix/client/v3/user/{uid}/account_data/m.secret_storage.key.{key_id}",
            headers=headers,
        ) as r:
            if r.status != 200:
                logger.warning("[key_backup] Cannot get SSSS key metadata (HTTP %d)", r.status)
                return 0
            key_metadata = await r.json()

        # ── Step 3: derive SSSS master key ──────────────────────────────────
        passphrase_info = key_metadata.get("passphrase")
        logger.info("[key_backup] key_metadata algorithm=%s has_passphrase=%s has_iv=%s has_mac=%s",
                    key_metadata.get("algorithm"), bool(passphrase_info),
                    bool(key_metadata.get("iv")), bool(key_metadata.get("mac")))
        try:
            if passphrase_info:
                algo = passphrase_info.get("algorithm", "")
                if algo != "m.pbkdf2":
                    logger.warning("[key_backup] Unsupported passphrase algorithm: %s", algo)
                    return 0
                ssss_key = _derive_ssss_key_from_passphrase(recovery_key, passphrase_info)
                logger.info("[key_backup] SSSS key derived via PBKDF2")
            else:
                ssss_key = _decode_recovery_key(recovery_key)
                logger.info("[key_backup] Recovery key decoded OK")
        except ValueError as e:
            logger.error("[key_backup] %s", e)
            return 0

        if not _verify_ssss_key(ssss_key, key_metadata):
            logger.warning("[key_backup] SSSS key pre-check failed, proceeding anyway (will fail at decrypt if key is wrong)")
        else:
            logger.info("[key_backup] SSSS key verified OK")

        # ── Step 4: encrypted backup private key ────────────────────────────
        async with http.get(
            f"{hs}/_matrix/client/v3/user/{uid}/account_data/m.megolm_backup.v1",
            headers=headers,
        ) as r:
            if r.status != 200:
                logger.warning("[key_backup] No m.megolm_backup.v1 account data (HTTP %d)", r.status)
                return 0
            backup_ad = await r.json()

        enc_for_key = backup_ad.get("encrypted", {}).get(key_id)
        if not enc_for_key:
            logger.warning("[key_backup] Backup key not encrypted for SSSS key %s", key_id)
            return 0

        # ── Step 5: decrypt backup private key ──────────────────────────────
        try:
            raw = _ssss_decrypt(ssss_key, "m.megolm_backup.v1", enc_for_key)
            raw_str = raw.decode("utf-8").strip()
            # Value may be JSON {"algorithm":..., "key":"base64"} or plain base64 string
            try:
                obj = json.loads(raw_str)
                b64_str = obj["key"] if isinstance(obj, dict) else raw_str
            except (json.JSONDecodeError, KeyError):
                b64_str = raw_str
            # Handle unpadded base64 (matrix-js-sdk sometimes omits trailing =)
            b64_str += "=" * (-len(b64_str) % 4)
            backup_priv = base64.b64decode(b64_str)
        except Exception as e:
            logger.error("[key_backup] Failed to decrypt backup key: %s", e)
            return 0
        if len(backup_priv) != 32:
            logger.error("[key_backup] Unexpected backup key length %d", len(backup_priv))
            return 0
        logger.info("[key_backup] Backup Curve25519 private key decrypted OK")

        # ── Step 6: backup version ───────────────────────────────────────────
        async with http.get(
            f"{hs}/_matrix/client/v3/room_keys/version", headers=headers
        ) as r:
            if r.status != 200:
                logger.warning("[key_backup] Cannot get backup version (HTTP %d)", r.status)
                return 0
            vdata = await r.json()
        version = vdata.get("version", "")
        algo = vdata.get("algorithm", "")
        if algo != "m.megolm_backup.v1.curve25519-aes-sha2":
            logger.warning("[key_backup] Unsupported backup algorithm: %s", algo)
            return 0
        logger.info("[key_backup] Backup version=%s", version)

        # ── Step 7: fetch all session keys ───────────────────────────────────
        async with http.get(
            f"{hs}/_matrix/client/v3/room_keys/keys?version={version}",
            headers=headers,
        ) as r:
            if r.status != 200:
                logger.warning("[key_backup] Cannot fetch room keys (HTTP %d)", r.status)
                return 0
            all_keys = await r.json()

    # ── Step 8: decrypt sessions ─────────────────────────────────────────────
    export_sessions: list[dict] = []
    n_failed = 0
    for room_id, room_data in all_keys.get("rooms", {}).items():
        for session_id, sinfo in room_data.get("sessions", {}).items():
            dec = _decrypt_backup_session(backup_priv, sinfo.get("session_data", {}))
            if dec is None:
                n_failed += 1
                continue
            export_sessions.append({
                "algorithm": "m.megolm.v1.aes-sha2",
                "forwarding_curve25519_key_chain": dec.get("forwarding_curve25519_key_chain", []),
                "room_id": room_id,
                "sender_claimed_ed25519_key": dec.get("sender_claimed_keys", {}).get("ed25519", ""),
                "sender_key": dec.get("sender_key", ""),
                "session_id": session_id,
                "session_key": dec.get("session_key", ""),
            })

    total = len(export_sessions)
    logger.info("[key_backup] Decrypted %d sessions, %d failed", total, n_failed)
    if not export_sessions:
        return 0

    # ── Step 9: write temp export file + import ──────────────────────────────
    tmp_pass = base64.b64encode(os.urandom(24)).decode()
    export_str = _create_key_export_data(export_sessions, tmp_pass)

    fd, tmp_path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(export_str)
        await client.import_keys(tmp_path, tmp_pass)
        logger.info("[key_backup] Successfully imported %d sessions into crypto store", total)
    except Exception as e:
        logger.error("[key_backup] import_keys failed: %s", e)
        return 0
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return total
