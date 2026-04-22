#!/usr/bin/env python3
"""Backfill historical messages from Matrix rooms into the local MessageStore.

Usage:
    python backfill.py [config.yaml] [options]

Options (via CLI flags or config bridge.message_store):
    --rooms ROOM_ID [...]    Only backfill these rooms (default: all joined rooms)
    --days N                 How many days of history to fetch (default: 30)
    --limit N                Max messages per room, 0 = unlimited (default: 0)
    --no-media               Skip media downloads
    --dry-run                Count messages without saving

The script reads source credentials from the same config.yaml used by the bridge.
It connects as the source bot, fetches historical events via /messages pagination,
decrypts encrypted events when possible, and stores them in the MessageStore SQLite
database (skipping duplicates by event_id).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import yaml
from nio import (
    AsyncClient,
    AsyncClientConfig,
    DownloadError,
    MegolmEvent,
    RedactionEvent,
    RoomEncryptedMedia,
    RoomMessage,
    RoomMessageAudio,
    RoomMessageEmote,
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageMedia,
    RoomMessageNotice,
    RoomMessageText,
    RoomMessageVideo,
    SyncResponse,
    WhoamiResponse,
)

from bridge.crypto import decrypt_config, is_encrypted
from bridge.message_store import MessageStore
from bridge.models import (
    BridgeMessage,
    CallAction,
    MessageDirection,
    MessageType,
)
from bridge.state import StateManager

logging.addLevelName(60, "ALWAYS")
logger = logging.getLogger("backfill")

MEDIA_MSGTYPES: dict[str, MessageType] = {
    "m.image": MessageType.IMAGE,
    "m.video": MessageType.VIDEO,
    "m.audio": MessageType.AUDIO,
    "m.file": MessageType.FILE,
}

BATCH_SIZE = 250


def setup_logging(level: str = "INFO") -> None:
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if root.handlers:
        root.handlers.clear()
    sh = logging.StreamHandler()
    sh.setLevel(root.level)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def _decrypt_config_if_needed(config: dict) -> dict:
    needs_key = False
    for section_key in ("source", "target"):
        section = config.get(section_key)
        if not isinstance(section, dict):
            continue
        for field in ("access_token", "password", "key_import_passphrase"):
            if is_encrypted(section.get(field, "")):
                needs_key = True
                break
        if needs_key:
            break
    if not needs_key:
        return config
    master_key = getpass.getpass("Enter master password to decrypt config: ")
    return decrypt_config(config, master_key)


def _get_room_name(room) -> str:
    if room.name:
        return room.name
    if room.canonical_alias:
        return room.canonical_alias
    display = room.display_name or ""
    if display and display.lower().replace(" ", "") not in ("emptyroom", "empty"):
        return display
    return room.room_id


def _get_sender_displayname(room, sender: str) -> str:
    user = room.users.get(sender)
    if user and user.display_name:
        return user.display_name
    return sender


async def _init_client(source_config: dict, state: StateManager) -> AsyncClient:
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

    access_token = source_config.get("access_token", "")
    if access_token:
        access_token = access_token.strip()
        client.restore_login(
            user_id=source_config["user_id"],
            device_id=source_config.get("device_id") or "",
            access_token=access_token,
        )
        logger.info("Restored login with access_token")
    else:
        password = source_config.get("password", "")
        if not password:
            password = getpass.getpass(f"Password for {source_config['user_id']}: ")
        resp = await client.login(password)
        if hasattr(resp, "access_token"):
            client.access_token = resp.access_token
            logger.info("Logged in successfully")
        else:
            raise RuntimeError(f"Login failed: {resp}")

    try:
        await client.keys_upload()
    except Exception:
        pass
    if client.should_query_keys:
        await client.keys_query()

    key_file = source_config.get("key_import_file", "")
    key_passphrase = source_config.get("key_import_passphrase", "")
    if key_file and key_passphrase:
        key_file = os.path.abspath(key_file)
        if os.path.isfile(key_file):
            try:
                await client.import_keys(key_file, key_passphrase)
                logger.info("Imported encryption keys from %s", key_file)
            except Exception as e:
                logger.warning("Failed to import keys: %s", e)

    resp = await client.whoami()
    if isinstance(resp, WhoamiResponse):
        logger.info("Auth verified: user=%s device=%s", resp.user_id, resp.device_id)
    else:
        raise RuntimeError(f"Auth check failed: {resp}")

    resp = await client.sync(timeout=30000, full_state=True)
    if isinstance(resp, SyncResponse):
        logger.info("Full sync done, %d rooms", len(client.rooms))
        if resp.next_batch:
            await state.save_sync_token("source", resp.next_batch)
    else:
        logger.warning("Full sync failed: %s, retrying without token ...", resp)
        client.next_batch = ""
        resp2 = await client.sync(timeout=30000, full_state=True)
        if isinstance(resp2, SyncResponse):
            logger.info("Retry sync done, %d rooms", len(client.rooms))
            if resp2.next_batch:
                await state.save_sync_token("source", resp2.next_batch)

    if client.should_query_keys:
        await client.keys_query()

    all_members: set[str] = set()
    for room_id, room in client.rooms.items():
        if room.encrypted:
            all_members.update(room.users.keys())
    if all_members:
        for uid in all_members:
            client.users_for_key_query.add(uid)
        try:
            await client.keys_query()
            logger.info("Queried device keys for %d members", len(all_members))
        except Exception as e:
            logger.warning("Failed to query member keys: %s", e)

    return client


def _parse_event_to_message(room, event, decrypted_body: str = "") -> Optional[BridgeMessage]:
    room_id = room.room_id
    room_name = _get_room_name(room)
    sender = event.sender
    sender_dn = _get_sender_displayname(room, sender)
    timestamp = event.server_timestamp
    event_id = event.event_id
    user_id = getattr(room, "own_user_id", "") or ""
    from_self = sender == user_id

    source = getattr(event, "source", {})
    if isinstance(source, dict):
        content = source.get("content", {})
        if not isinstance(content, dict):
            content = {}
    else:
        content = {}

    relates_to = content.get("m.relates_to", {}) if isinstance(content, dict) else {}
    new_content = content.get("m.new_content") if isinstance(content, dict) else None

    if relates_to.get("rel_type") == "m.replace" and new_content and isinstance(new_content, dict):
        edited_text = new_content.get("body", "")
        if not edited_text:
            edited_text = (getattr(event, "body", "") or "").lstrip("* ")
        return BridgeMessage(
            source_room_id=room_id,
            source_room_name=room_name,
            sender=sender,
            sender_displayname=sender_dn,
            text=edited_text,
            timestamp=timestamp,
            event_id=event_id,
            backend_name="backfill",
            direction=MessageDirection.EDIT,
            msgtype=MessageType.TEXT,
            edit_of_event_id=relates_to.get("event_id", ""),
            from_self=from_self,
        )

    if isinstance(event, RoomMessageText):
        msgtype = MessageType.TEXT
    elif isinstance(event, RoomMessageNotice):
        msgtype = MessageType.NOTICE
    elif isinstance(event, RoomMessageEmote):
        msgtype = MessageType.EMOTE
    elif isinstance(event, RoomMessageImage):
        msgtype = MessageType.IMAGE
    elif isinstance(event, RoomMessageVideo):
        msgtype = MessageType.VIDEO
    elif isinstance(event, RoomMessageAudio):
        msgtype = MessageType.AUDIO
    elif isinstance(event, RoomMessageFile):
        msgtype = MessageType.FILE
    elif isinstance(event, (RoomMessageMedia, RoomEncryptedMedia)):
        raw_msgtype = getattr(event, "msgtype", "m.file")
        msgtype = MEDIA_MSGTYPES.get(raw_msgtype, MessageType.FILE)
    else:
        msgtype = MessageType.TEXT

    text = getattr(event, "body", "") or decrypted_body or ""
    media_url = getattr(event, "url", None)
    is_media = isinstance(event, (RoomMessageImage, RoomMessageVideo, RoomMessageAudio, RoomMessageFile, RoomMessageMedia, RoomEncryptedMedia))
    media_filename = getattr(event, "body", "") if (media_url or is_media) else ""
    info: dict = {}
    if hasattr(event, "media_info") and isinstance(event.media_info, dict):
        info = event.media_info
    if not info and isinstance(source, dict):
        info = content.get("info", {})
        if not isinstance(info, dict):
            info = {}

    return BridgeMessage(
        source_room_id=room_id,
        source_room_name=room_name,
        sender=sender,
        sender_displayname=sender_dn,
        text=text,
        timestamp=timestamp,
        event_id=event_id,
        backend_name="backfill",
        direction=MessageDirection.FORWARD,
        msgtype=msgtype,
        media_url=media_url or "",
        media_data=None,
        media_mimetype=info.get("mimetype", "application/octet-stream"),
        media_filename=media_filename,
        media_size=info.get("size", 0),
        from_self=from_self,
    )


async def _download_media_for(client: AsyncClient, event, max_size: int) -> Optional[bytes]:
    media_url = getattr(event, "url", None)
    if not media_url:
        return None
    try:
        resp = await client.download(mxc=media_url)
        if isinstance(resp, DownloadError):
            return None
        data = resp.body
        if len(data) > max_size:
            return None
        if isinstance(event, RoomEncryptedMedia):
            try:
                from nio.crypto.attachments import decrypt_attachment
                data = decrypt_attachment(
                    data,
                    key=event.key["k"],
                    hash=event.hashes.get("sha256", ""),
                    iv=event.iv,
                )
            except Exception as e:
                logger.warning("Failed to decrypt media %s: %s", media_url, e)
                return None
        return data
    except Exception:
        return None


def _apply_redactions(store: Optional[MessageStore], redacted_ids: set[str], args: argparse.Namespace) -> int:
    if not redacted_ids or args.dry_run or not store:
        return 0
    deleted = 0
    for rid in redacted_ids:
        if store.delete_message(rid):
            deleted += 1
    return deleted


async def backfill_room(
    client: AsyncClient,
    store: Optional[MessageStore],
    room_id: str,
    room_name: str,
    args: argparse.Namespace,
) -> int:
    room = client.rooms.get(room_id)
    if not room:
        logger.warning("Room %s not found in joined rooms, skipping", room_id)
        return 0

    cutoff_ts = 0
    if args.days > 0:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
        cutoff_ts = cutoff_dt.timestamp() * 1000

    saved = 0
    skipped = 0
    decrypted_ok = 0
    decrypted_fail = 0
    media_downloaded = 0
    redacted_ids: set[str] = set()
    end_token = None
    batch = 0
    max_size = args.media_max_size

    while True:
        batch += 1
        resp = await client.room_messages(
            room_id=room_id,
            start=end_token,
            limit=BATCH_SIZE,
            direction="b",
        )
        if not hasattr(resp, "chunk"):
            logger.warning("room_messages returned unexpected response for %s", room_id)
            break

        events = resp.chunk
        end_token = getattr(resp, "end", None)
        if not events:
            break

        for event in events:
            if cutoff_ts and hasattr(event, "server_timestamp"):
                if event.server_timestamp < cutoff_ts:
                    logger.info(
                        "[%s] Reached cutoff date (%s), stopping",
                        room_name, datetime.fromtimestamp(cutoff_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                    )
                    rc = _apply_redactions(store, redacted_ids, args)
                    return max(saved - rc, 0)

            if not hasattr(event, "event_id") or not event.event_id:
                continue

            if isinstance(event, RedactionEvent):
                redacted = getattr(event, "redacts", "")
                if redacted:
                    redacted_ids.add(redacted)
                continue

            if not isinstance(event, (RoomMessage, MegolmEvent)):
                continue

            if isinstance(event, MegolmEvent):
                try:
                    event = await client.decrypt_event(event)
                    decrypted_ok += 1
                except Exception as e:
                    decrypted_fail += 1
                    if decrypted_fail <= 5:
                        logger.debug("Failed to decrypt %s: %s", event.event_id, e)
                    continue
                if not isinstance(event, RoomMessage):
                    continue

            if store and store.event_id_exists(event.event_id):
                skipped += 1
                continue

            msg = _parse_event_to_message(room, event)
            if msg is None:
                continue

            if store and not args.dry_run:
                if not args.no_media and msg.media_url and msgtype_is_media(msg.msgtype):
                    data = await _download_media_for(client, event, max_size)
                    if data:
                        msg.media_data = data
                        msg.media_size = len(data)
                        media_downloaded += 1
                store.save_message(msg, args.media_dir if not args.no_media else "")
                if msg.sender_displayname and msg.sender_displayname != msg.sender:
                    store.upsert_user_alias(msg.sender, msg.sender_displayname)
                if msg.source_room_name and msg.source_room_name != msg.source_room_id:
                    store.upsert_room_alias(msg.source_room_id, msg.source_room_name)

            saved += 1

        if saved > 0 and saved % 500 == 0:
            logger.info("[%s] Progress: %d saved, %d skipped", room_name, saved, skipped)

        if args.limit > 0 and saved >= args.limit:
            logger.info("[%s] Reached message limit (%d)", room_name, args.limit)
            break

        if not end_token:
            break

    redacted_count = _apply_redactions(store, redacted_ids, args)
    logger.info(
        "[%s] Done: %d saved, %d skipped (dup), %d decrypted, %d decrypt-failed, %d media, %d redacted",
        room_name, saved, skipped, decrypted_ok, decrypted_fail, media_downloaded, redacted_count,
    )
    return saved - redacted_count


def msgtype_is_media(msgtype: MessageType) -> bool:
    return msgtype in (MessageType.IMAGE, MessageType.VIDEO, MessageType.AUDIO, MessageType.FILE)


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="Backfill Matrix room history into MessageStore")
    parser.add_argument("config", nargs="?", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--rooms", nargs="+", default=[], help="Room IDs to backfill (default: all)")
    parser.add_argument("--days", type=int, default=30, help="Days of history to fetch (0=all, default=30)")
    parser.add_argument("--limit", type=int, default=0, help="Max messages per room (0=unlimited)")
    parser.add_argument("--no-media", action="store_true", help="Skip media downloads")
    parser.add_argument("--dry-run", action="store_true", help="Count only, don't save")
    parser.add_argument("--log-level", default="INFO", help="Log level (default=INFO)")
    args = parser.parse_args()

    setup_logging(args.log_level)

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)

    config = _decrypt_config_if_needed(config)

    source_config = config.get("source", {})
    if not source_config.get("homeserver"):
        logger.error("source.homeserver is required in config")
        sys.exit(1)

    bridge_config = config.get("bridge", {})
    store_cfg = bridge_config.get("message_store", {})
    store_path = store_cfg.get("path", "messages.db")

    if not store_cfg.get("enabled", False) and not args.dry_run:
        logger.info("message_store not enabled in config, but continuing for backfill")

    state = StateManager(bridge_config.get("state_path", "state.json"))
    await state.load()

    logger.info("Connecting to Matrix as %s ...", source_config.get("user_id"))
    client = await _init_client(source_config, state)

    store = None
    if not args.dry_run:
        store = MessageStore(store_path, media_dir=store_cfg.get("media_dir", ""))
        logger.info("MessageStore opened: %s", store_path)

    args.media_dir = store_cfg.get("media_dir", "") if not args.no_media else ""
    args.media_max_size = source_config.get("media_max_size", 50 * 1024 * 1024)

    if args.media_dir and not args.no_media:
        os.makedirs(args.media_dir, exist_ok=True)

    joined_rooms = dict(client.rooms)
    logger.info("Bot has joined %d room(s):", len(joined_rooms))
    for rid, room in joined_rooms.items():
        logger.info("  - %s (%s)", _get_room_name(room), rid)

    if args.rooms:
        target_rooms = {rid: joined_rooms[rid] for rid in args.rooms if rid in joined_rooms}
        missing = [rid for rid in args.rooms if rid not in joined_rooms]
        if missing:
            logger.warning("Rooms not found (not joined): %s", missing)
    else:
        target_rooms = joined_rooms

    if not target_rooms:
        logger.error("No rooms to backfill. The bot may not have joined any rooms, or the specified room IDs don't match.")
        await client.close()
        return

    logger.info("Rooms to backfill: %d", len(target_rooms))
    total_saved = 0
    start_time = time.time()

    for i, (room_id, room) in enumerate(target_rooms.items(), 1):
        room_name = _get_room_name(room)
        logger.info("[%d/%d] Backfilling %s (%s) ...", i, len(target_rooms), room_name, room_id)
        try:
            count = await backfill_room(client, store, room_id, room_name, args)
            total_saved += count
        except Exception as e:
            logger.error("Failed to backfill %s: %s", room_name, e, exc_info=True)

    elapsed = time.time() - start_time
    logger.info(
        "Backfill complete: %d messages saved across %d rooms in %.1fs",
        total_saved, len(target_rooms), elapsed,
    )

    try:
        if store and not args.dry_run:
            logger.info("Reconciling edits ...")
            edits_applied = store.reconcile_edits()
            logger.info("Reconciled %d edits", edits_applied)

        await state.save_sync_token("source", client.next_batch)
        await state.flush()
    finally:
        await client.close()
        if store:
            store.close()
    logger.info("Done")


if __name__ == "__main__":
    asyncio.run(main_async())
