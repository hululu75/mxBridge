from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Optional

from nio import (
    AsyncClient,
    MegolmEvent,
    RoomEncryptedMedia,
    RoomMessage,
    RoomMessageMedia,
    RoomMessageText,
    SyncResponse,
)

from backends.matrix_base import ALWAYS, MEDIA_MSGTYPES, MatrixBackend
from bridge.models import BridgeMessage, MessageDirection, MessageType
from bridge.state import StateManager

logger = logging.getLogger(__name__)


class MatrixTargetBackend(MatrixBackend):
    def __init__(
        self,
        name: str,
        config: dict,
        state: StateManager,
        command_prefix: str = "!send",
        *,
        config_path: Optional[str] = None,
    ) -> None:
        super().__init__(name, config, state, config_path=config_path)
        self._command_prefix = command_prefix
        self._control_prefix = command_prefix[0] if command_prefix else "!"
        self._control_commands = {
            f"{self._control_prefix}login",
            f"{self._control_prefix}logout",
            f"{self._control_prefix}pause",
            f"{self._control_prefix}resume",
            f"{self._control_prefix}status",
        }
        self.target_room: str = config.get("target_room", "")

    async def start(self) -> None:
        saved_token = await self._init_client()

        client = self._get_client()
        client.add_event_callback(self._on_room_event, RoomMessage)
        client.add_event_callback(self._on_encrypted_event, MegolmEvent)
        self._register_common_callbacks(client)

        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush())
        self._key_upload_task = asyncio.create_task(self._periodic_key_upload())

        logger.info("[%s] Performing initial sync to load rooms", self.name)
        resp = await client.sync(timeout=0, full_state=True)
        if isinstance(resp, SyncResponse) and resp.next_batch:
            await self._state.save_sync_token(self.name, resp.next_batch)

        if saved_token:
            client.next_batch = saved_token
            logger.info("[%s] Resumed from sync token", self.name)

        await self._query_room_members()

        if self.target_room in client.rooms:
            logger.log(ALWAYS, "[%s] Target room %s is ready", self.name, self.target_room)
        else:
            logger.warning("[%s] Target room %s not found after initial sync", self.name, self.target_room)

        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.log(ALWAYS, "[%s] Started, monitoring target room: %s", self.name, self.target_room)

    # -------------------------------------------------- sync hook

    async def _after_sync(self, client: AsyncClient, resp: SyncResponse) -> None:
        await self._check_undecrypted_events(client, resp)

    # -------------------------------------------------- room helpers

    async def _query_room_members(self) -> None:
        """Query device keys for all members of the target room so device_store
        is populated before encrypted messages arrive."""
        client = self._get_client()
        if not self.target_room or self.target_room not in client.rooms:
            return
        room = client.rooms[self.target_room]
        members = list(room.users.keys())
        if not members:
            return
        try:
            for uid in members:
                client.users_for_key_query.add(uid)
            await client.keys_query()
            logger.info("[%s] Queried device keys for %d room members", self.name, len(members))
        except Exception as e:
            logger.warning("[%s] Failed to query room member keys: %s", self.name, e)

    async def _check_undecrypted_events(self, client: AsyncClient, resp: SyncResponse) -> None:
        if not self.target_room:
            return
        for room_id, room_info in resp.rooms.join.items():
            if room_id != self.target_room:
                continue
            timeline_events = getattr(room_info, "timeline", None)
            if not timeline_events:
                continue
            for event in getattr(timeline_events, "events", []):
                logger.debug(
                    "[%s] Sync timeline event: type=%s sender=%s event_id=%s",
                    self.name, type(event).__name__,
                    getattr(event, "sender", "?"), getattr(event, "event_id", "?"),
                )
                if not isinstance(event, MegolmEvent):
                    continue
                if self._state.is_processed(event.event_id):
                    continue
                sender_device = getattr(event, "device_id", None)
                own_device = self.config.get("device_id", "BRIDGE_TARGET")
                if event.sender == self.config["user_id"] and sender_device == own_device:
                    continue
                await self._state.mark_processed(event.event_id)
                room = client.rooms.get(room_id)
                sender_displayname = await self._get_sender_displayname(room, event.sender) if room else event.sender
                logger.warning(
                    "[%s] Detected undecrypted megolm event %s from %s",
                    self.name, event.event_id, event.sender,
                )
                try:
                    await self.send_message(
                        room_id,
                        f"⛔ Unable to decrypt message from {sender_displayname}",
                        "m.notice",
                    )
                except Exception:
                    logger.error("[%s] Failed to send decryption failure notice", self.name)

    # -------------------------------------------------- event callbacks

    async def _on_room_event(self, room, event) -> None:
        if event.sender == self.config["user_id"]:
            sender_device = getattr(event, "device_id", None)
            if not sender_device:
                sender_device = event.source.get("content", {}).get("device_id")
            own_device = self.config.get("device_id", "BRIDGE_TARGET")
            # Filter our own device; also filter if device_id is indeterminate (conservative)
            if sender_device is None or sender_device == own_device:
                return
            logger.info(
                "[%s] Event from own user but different device: sender_device=%s own_device=%s",
                self.name, sender_device, own_device,
            )
        if self.target_room and room.room_id != self.target_room:
            return
        if self._state.is_processed(event.event_id):
            return
        await self._state.mark_processed(event.event_id)

        if isinstance(event, RoomMessageText):
            await self._handle_text(room, event)
        elif isinstance(event, RoomMessageMedia):
            await self._handle_media(room, event)
        elif hasattr(event, "url") and event.url:
            await self._handle_media(room, event)

    async def _on_encrypted_event(self, room, event) -> None:
        if event.sender == self.config["user_id"]:
            sender_device = getattr(event, "device_id", None) or event.source.get("content", {}).get("device_id")
            own_device = self.config.get("device_id", "BRIDGE_TARGET")
            if sender_device is None or sender_device == own_device:
                return
        if self.target_room and room.room_id != self.target_room:
            return
        if self._state.is_processed(event.event_id):
            return

        try:
            decrypted = await self._get_client().decrypt_event(event)
        except Exception as e:
            logger.warning("[%s] Failed to decrypt: %s", self.name, e)
            await self._state.mark_processed(event.event_id)
            sender_displayname = await self._get_sender_displayname(room, event.sender)
            try:
                await self.send_message(
                    room.room_id,
                    f"⛔ Unable to decrypt message from {sender_displayname}",
                    "m.notice",
                )
            except Exception:
                logger.error("[%s] Failed to send decryption failure notice", self.name)
            if self.config.get("handle_encrypted", True):
                await self._enqueue_pending_encrypted(room, event, e)
            return

        await self._dispatch_decrypted(room, event.event_id, decrypted)

    async def _on_room_key_received(self, event) -> None:
        """Retry decryption of pending events when a new megolm session key arrives."""
        session_id = getattr(event, "session_id", None)
        if not session_id or session_id not in self._pending_encrypted:
            return
        entry = self._pending_encrypted.pop(session_id)
        pending = entry["events"]
        logger.info(
            "[%s] Room key arrived for session %s, retrying %d pending event(s)",
            self.name, session_id, len(pending),
        )
        client = self._get_client()
        for room, enc_event in pending:
            try:
                decrypted = await client.decrypt_event(enc_event)
                await self._dispatch_decrypted(room, enc_event.event_id, decrypted)
            except Exception as e:
                logger.warning("[%s] Retry decrypt failed for %s: %s", self.name, enc_event.event_id, e)

    # -------------------------------------------------- message handlers

    async def _handle_text(self, room, event, original_event_id: Optional[str] = None) -> None:
        body = event.body or ""
        reply_to_id = self._get_reply_to_event_id(event)

        if reply_to_id:
            text = self._strip_reply_quote(body)
            msg = BridgeMessage(
                source_room_id=room.room_id,
                source_room_name="target",
                sender=event.sender,
                sender_displayname=await self._get_sender_displayname(room, event.sender),
                text=text,
                timestamp=event.server_timestamp,
                event_id=original_event_id or event.event_id,
                backend_name=self.name,
                direction=MessageDirection.REPLY,
                msgtype=MessageType.TEXT,
                target_room_id="",
                reply_to_event_id=reply_to_id,
            )
            await self._emit_message(msg)
            return

        stripped = body.strip()
        if stripped in self._control_commands:
            action = stripped[len(self._control_prefix):]
            msg = BridgeMessage(
                source_room_id=room.room_id,
                source_room_name="target",
                sender=event.sender,
                sender_displayname=await self._get_sender_displayname(room, event.sender),
                text=action,
                timestamp=event.server_timestamp,
                event_id=original_event_id or event.event_id,
                backend_name=self.name,
                direction=MessageDirection.CONTROL,
                msgtype=MessageType.TEXT,
            )
            await self._emit_message(msg)
            return

        if not body.startswith(self._command_prefix):
            return

        command_body = body[len(self._command_prefix):].strip()
        parsed = self._parse_command(command_body)
        if not parsed:
            await self.send_message(
                room.room_id,
                f"Usage: {self._command_prefix} #room_alias_or_id message text",
            )
            return

        target_room, message_text = parsed
        msg = BridgeMessage(
            source_room_id=room.room_id,
            source_room_name="target",
            sender=event.sender,
            sender_displayname=await self._get_sender_displayname(room, event.sender),
            text=message_text,
            timestamp=event.server_timestamp,
            event_id=original_event_id or event.event_id,
            backend_name=self.name,
            direction=MessageDirection.REPLY,
            msgtype=MessageType.TEXT,
            target_room_id=target_room,
        )
        await self._emit_message(msg)

    async def _handle_media(self, room, event, original_event_id: Optional[str] = None) -> None:
        msgtype_str = getattr(event, "msgtype", "m.file")
        msgtype = MEDIA_MSGTYPES.get(msgtype_str, MessageType.FILE)
        media_url, info, data = await self._download_media(event)
        msg = BridgeMessage(
            source_room_id=room.room_id,
            source_room_name="target",
            sender=event.sender,
            sender_displayname=await self._get_sender_displayname(room, event.sender),
            text=event.body or "",
            timestamp=event.server_timestamp,
            event_id=original_event_id or event.event_id,
            backend_name=self.name,
            direction=MessageDirection.REPLY,
            msgtype=msgtype,
            media_url=media_url,
            media_data=data,
            media_mimetype=info.get("mimetype", "application/octet-stream"),
            media_filename=getattr(event, "body", "file"),
            media_size=info.get("size", len(data) if data else 0),
            media_width=info.get("w"),
            media_height=info.get("h"),
            media_duration=info.get("duration"),
            reply_to_event_id=self._get_reply_to_event_id(event),
        )
        await self._emit_message(msg)

    async def _dispatch_decrypted(self, room, original_event_id: str, decrypted) -> None:
        if not isinstance(decrypted, RoomMessage):
            return
        await self._state.mark_processed(original_event_id)
        if isinstance(decrypted, RoomMessageText):
            await self._handle_text(room, decrypted, original_event_id=original_event_id)
        elif isinstance(decrypted, (RoomMessageMedia, RoomEncryptedMedia)):
            await self._handle_media(room, decrypted, original_event_id=original_event_id)
        elif hasattr(decrypted, "url") and decrypted.url:
            await self._handle_media(room, decrypted, original_event_id=original_event_id)

    # -------------------------------------------------- command parsing

    def _get_reply_to_event_id(self, event) -> Optional[str]:
        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        if isinstance(relates_to, dict):
            in_reply_to = relates_to.get("m.in_reply_to", {})
            if isinstance(in_reply_to, dict):
                return in_reply_to.get("event_id")
        return None

    @staticmethod
    def _strip_reply_quote(body: str) -> str:
        """Strip only the leading Matrix reply fallback block (lines starting with '> ').

        The Matrix spec places the quoted fallback at the top, separated from
        the actual reply by a blank line.  Only that leading block is stripped
        so user-authored markdown blockquotes elsewhere in the message are
        preserved."""
        lines = body.split("\n")
        i = 0
        while i < len(lines) and lines[i].startswith("> "):
            i += 1
        # Skip the blank separator line that follows the quote block
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        return "\n".join(lines[i:]).strip()

    def _parse_command(self, body: str) -> Optional[tuple[str, str]]:
        try:
            parts = shlex.split(body)
        except ValueError:
            parts = body.split(None, 1)

        if not parts:
            return None

        target_room = parts[0]
        message_text = " ".join(parts[1:]) if len(parts) > 1 else ""
        if not message_text:
            return None

        if not target_room.startswith(("#", "!")):
            return None

        return target_room, message_text

    # -------------------------------------------------- extra send methods

    async def send_message(self, room_id: str, text: str, msgtype: str = "m.notice") -> str:
        """Override to use m.notice as the default message type."""
        return await super().send_message(room_id, text, msgtype)

    async def get_event_body(self, room_id: str, event_id: str) -> Optional[str]:
        client = self._get_client()
        try:
            resp = await client.room_get_event(room_id, event_id)
            if hasattr(resp, "event") and hasattr(resp.event, "body"):
                return resp.event.body
            if hasattr(resp, "event") and hasattr(resp.event, "source"):
                content = resp.event.source.get("content", {})
                if isinstance(content, dict):
                    return content.get("body")
        except Exception as e:
            logger.debug("[%s] Failed to get event body: %s", self.name, e)
        return None

    async def send_reaction(self, room_id: str, event_id: str, key: str = "✓") -> str:
        client = self._get_client()
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": key,
            }
        }
        resp = await client.room_send(
            room_id=room_id,
            message_type="m.reaction",
            content=content,
            ignore_unverified_devices=True,
        )
        if hasattr(resp, "event_id"):
            return resp.event_id
        return ""
