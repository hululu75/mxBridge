#!/usr/bin/env python3
"""Repair corrupted encrypted media files in the MessageStore.

Scans media files saved by the bridge, detects corrupted ones (encrypted
ciphertext saved without decryption), re-downloads and decrypts them from
the Matrix server, then replaces the corrupted files.

Usage:
    python repair_media.py [config.yaml] [--dry-run]
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import asyncio
import getpass
import logging
import tempfile
from typing import Optional

import yaml
from nio import (
    AsyncClient,
    AsyncClientConfig,
    DownloadError,
    RoomEncryptedMedia,
    RoomGetEventError,
    SyncResponse,
    WhoamiResponse,
)
from nio.crypto.attachments import decrypt_attachment

from bridge.crypto import decrypt_config, is_encrypted
from bridge.message_store import MessageStore

logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("repair_media")

# Magic bytes for common media formats (offset, bytes)
_MAGIC_SIGNATURES: list[tuple[int, bytes]] = [
    # Images
    (0, b"\x89PNG"),
    (0, b"\xff\xd8\xff"),
    (0, b"GIF87a"),
    (0, b"GIF89a"),
    (8, b"WEBP"),           # RIFF....WEBP
    (0, b"BM"),             # BMP
    (0, b"\x00\x00\x01\x00"),  # ICO
    # Video
    (4, b"ftyp"),           # MP4/M4V/MOV/3GP
    (0, b"\x1a\x45\xdf\xa3"),  # WebM/MKV
    (0, b"RIFF"),           # AVI / WAV
    (0, b"\x00\x00\x01\xba"),  # MPEG-PS
    (0, b"\x00\x00\x01\xb3"),  # MPEG video
    (0, b"FLV\x01"),        # FLV
    # Audio
    (0, b"ID3"),            # MP3 with ID3 tag
    (0, b"\xff\xfb"),       # MP3 frame
    (0, b"\xff\xf3"),       # MP3 frame
    (0, b"\xff\xf2"),       # MP3 frame
    (0, b"fLaC"),           # FLAC
    (0, b"OggS"),           # Ogg (Vorbis/Opus)
    (0, b"FORM"),           # AIFF
    # Documents / archives
    (0, b"%PDF"),           # PDF
    (0, b"PK\x03\x04"),     # ZIP / DOCX / XLSX / APK
    (0, b"\x1f\x8b"),       # gzip
    (0, b"Rar!"),           # RAR
    (0, b"\xd0\xcf\x11\xe0"),  # OLE2 (DOC/XLS/PPT)
]


def _is_valid_media(path: str) -> bool:
    """Return True if the file starts with a recognised format magic signature."""
    try:
        with open(path, "rb") as f:
            header = f.read(16)
        for offset, magic in _MAGIC_SIGNATURES:
            if header[offset:offset + len(magic)] == magic:
                return True
        return False
    except OSError:
        return False


def _load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _decrypt_config_if_needed(config: dict) -> dict:
    needs_key = any(
        is_encrypted(config.get(sec, {}).get(field, ""))
        for sec in ("source", "target")
        for field in ("access_token", "password", "key_import_passphrase")
    )
    if not needs_key:
        return config
    master_key = os.environ.get("MXBIRDGE_MASTER_KEY") or getpass.getpass("Enter master password to decrypt config: ")
    return decrypt_config(config, master_key)


async def _init_client(source_config: dict) -> AsyncClient:
    store_path = os.path.abspath(source_config.get("store_path", "./store/source"))
    os.makedirs(store_path, exist_ok=True)
    client_config = AsyncClientConfig(
        store_sync_tokens=True,
        encryption_enabled=source_config.get("handle_encrypted", True),
    )
    client = AsyncClient(
        homeserver=source_config["homeserver"],
        user=source_config["user_id"],
        device_id=source_config.get("device_id") or "",
        store_path=store_path,
        config=client_config,
    )
    access_token = (source_config.get("access_token") or "").strip()
    if access_token:
        client.restore_login(
            user_id=source_config["user_id"],
            device_id=source_config.get("device_id") or "",
            access_token=access_token,
        )
    else:
        password = source_config.get("password") or getpass.getpass(
            f"Password for {source_config['user_id']}: "
        )
        resp = await client.login(password)
        if not hasattr(resp, "access_token"):
            raise RuntimeError(f"Login failed: {resp}")

    resp = await client.whoami()
    if not isinstance(resp, WhoamiResponse):
        raise RuntimeError(f"Auth check failed: {resp}")
    logger.info("Authenticated as %s", resp.user_id)

    resp = await client.sync(timeout=30000, full_state=True)
    if isinstance(resp, SyncResponse):
        logger.info("Sync done")

    key_file = source_config.get("key_import_file", "")
    key_pass = source_config.get("key_import_passphrase", "")
    if key_file and key_pass and os.path.isfile(os.path.abspath(key_file)):
        try:
            await client.import_keys(os.path.abspath(key_file), key_pass)
            logger.info("Imported encryption keys")
        except Exception as e:
            logger.warning("Key import failed: %s", e)

    if client.should_query_keys:
        await client.keys_query()

    return client


async def repair_one(
    client: AsyncClient,
    media_dir: str,
    event_id: str,
    room_id: str,
    local_path: str,
    dry_run: bool,
) -> str:
    """Return 'ok', 'skipped', 'failed', or 'not_encrypted'."""
    full_path = os.path.realpath(os.path.join(media_dir, local_path))
    if not full_path.startswith(os.path.realpath(media_dir) + os.sep):
        logger.warning("Path outside media_dir, skipping: %s", local_path)
        return "failed"
    if not os.path.isfile(full_path):
        return "missing"

    if _is_valid_media(full_path):
        return "ok"

    # File is corrupted – fetch the original Matrix event to get key material
    resp = await client.room_get_event(room_id, event_id)
    if isinstance(resp, RoomGetEventError):
        logger.warning("Could not fetch event %s: %s", event_id, resp)
        return "failed"

    event = resp.event
    if not isinstance(event, RoomEncryptedMedia):
        # Not an encrypted media event; corruption has another cause
        return "not_encrypted"

    media_url = getattr(event, "url", None)
    if not media_url:
        return "failed"

    dl = await client.download(mxc=media_url)
    if isinstance(dl, DownloadError):
        logger.warning("Download failed for %s: %s", event_id, dl)
        return "failed"

    try:
        plaintext = decrypt_attachment(
            dl.body,
            key=event.key["k"],
            hash=event.hashes.get("sha256", ""),
            iv=event.iv,
        )
    except Exception as e:
        logger.warning("Decrypt failed for %s: %s", event_id, e)
        return "failed"

    if dry_run:
        logger.info("[dry-run] Would replace %s (%d bytes)", local_path, len(plaintext))
        return "ok"

    dest_dir = os.path.dirname(full_path)
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".tmp_repair_")
    try:
        os.write(fd, plaintext)
        os.close(fd)
        os.chmod(tmp, 0o644)
        os.replace(tmp, full_path)
    except Exception as e:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        logger.error("Write failed for %s: %s", local_path, e)
        return "failed"

    logger.info("Repaired %s (%d bytes)", local_path, len(plaintext))
    return "ok"


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="Repair corrupted encrypted media files")
    parser.add_argument("config", nargs="?", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be repaired, don't write")
    args = parser.parse_args()

    try:
        config = _load_config(args.config)
    except FileNotFoundError:
        logger.error("Config not found: %s", args.config)
        sys.exit(1)

    config = _decrypt_config_if_needed(config)
    source_config = config.get("source", {})
    bridge_config = config.get("bridge", {})
    store_cfg = bridge_config.get("message_store", {})
    db_path = store_cfg.get("path", "messages.db")
    media_dir = store_cfg.get("media_dir", "")

    if not media_dir:
        logger.error("bridge.message_store.media_dir not configured")
        sys.exit(1)
    media_dir = os.path.abspath(media_dir)

    store = MessageStore(db_path)

    # Fetch all messages that have a local media file
    from bridge.message_store import db, Message
    rows = list(
        Message.select(
            Message.event_id,
            Message.source_room_id,
            Message.media_local_path,
            Message.msgtype,
        ).where(
            (Message.media_local_path != "") &
            (Message.msgtype.in_(["m.image", "m.video", "m.audio", "m.file"]))
        )
    )

    if not rows:
        logger.info("No media records found in database")
        store.close()
        return

    logger.info("Found %d media records, scanning for corrupted files...", len(rows))

    # Count corrupted files before connecting to Matrix
    corrupted = [
        r for r in rows
        if not _is_valid_media(os.path.join(media_dir, r.media_local_path))
        and os.path.isfile(os.path.join(media_dir, r.media_local_path))
    ]
    image_corrupted = [r for r in corrupted if r.msgtype == "m.image"]

    logger.info(
        "%d corrupted files found (%d images), %d appear valid",
        len(corrupted), len(image_corrupted),
        len(rows) - len(corrupted),
    )

    if not corrupted:
        logger.info("Nothing to repair")
        store.close()
        return

    logger.info("Connecting to Matrix...")
    client = await _init_client(source_config)

    stats = {"ok": 0, "failed": 0, "skipped": 0, "not_encrypted": 0, "missing": 0}
    for r in corrupted:
        result = await repair_one(
            client, media_dir,
            r.event_id, r.source_room_id, r.media_local_path,
            args.dry_run,
        )
        stats[result] = stats.get(result, 0) + 1

    await client.close()
    store.close()

    logger.info(
        "Done: %d repaired, %d failed, %d not-encrypted, %d missing",
        stats.get("ok", 0), stats.get("failed", 0),
        stats.get("not_encrypted", 0), stats.get("missing", 0),
    )


if __name__ == "__main__":
    asyncio.run(main_async())
