from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from backends.base import BaseBackend
from backends.matrix_base import ALWAYS
from bridge.message_store import MessageStore
from bridge.models import (
    BridgeMessage,
    CallAction,
    MessageDirection,
    MessageType,
)

logger = logging.getLogger(__name__)

DEFAULT_MESSAGE_FORMAT = "[{room_name}] {sender}: {text}"
MEDIA_MSGTYPE_MAP = {
    MessageType.IMAGE: "m.image",
    MessageType.VIDEO: "m.video",
    MessageType.AUDIO: "m.audio",
    MessageType.FILE: "m.file",
}


class BridgeCore:
    def __init__(
        self,
        source: BaseBackend,
        target: Optional[BaseBackend],
        bridge_config: dict,
        state=None,
        message_store: Optional[MessageStore] = None,
    ):
        self._source = source
        self._target = target
        self._backup_mode = target is None
        self._config = bridge_config
        self._message_format = bridge_config.get("message_format", DEFAULT_MESSAGE_FORMAT)
        self._command_prefix = bridge_config.get("command_prefix", "!send")
        self._media_enabled = bridge_config.get("media", {}).get("enabled", True)
        self._call_enabled = bridge_config.get("call_notifications", {}).get("enabled", True)
        self._state = state
        self._store = message_store
        self._media_dir = ""
        if self._store:
            store_cfg = bridge_config.get("message_store", {})
            self._media_dir = store_cfg.get("media_dir", "")
        self._room_id_map: dict[str, str] = {}
        self._source_to_target_map: dict[str, str] = {}
        self._forwarding_enabled = False
        self._forwarding_paused = False
        self._source_started = False
        self._shutdown_event = asyncio.Event()
        self._control_lock = asyncio.Lock()
        self._admin_users: set[str] = set(bridge_config.get("admin_users", []))
        self._target_room: str = getattr(target, "target_room", "") if target else ""

    def _format_message(self, msg: BridgeMessage) -> str:
        if msg.msgtype == MessageType.EMOTE:
            text = f"* {msg.sender_displayname} {msg.text}"
        else:
            text = msg.text
        return self._message_format.format(
            room_name=msg.source_room_name,
            sender=msg.sender_displayname,
            text=text,
        )

    async def start(self) -> None:
        self._source.on_message(self._on_source_message)
        if self._media_dir:
            os.makedirs(self._media_dir, exist_ok=True)
            logger.info("Media files will be saved to %s", self._media_dir)
        if self._backup_mode:
            logger.info("BridgeCore starting source backend (backup mode)")
            await self._source.start()
            self._source_started = True
            self._forwarding_enabled = True
            logger.log(ALWAYS, "Backup mode active: saving messages to store (no forwarding)")
            return
        assert self._target is not None
        self._target.on_message(self._on_target_message)
        logger.info("BridgeCore starting target backend")
        await self._target.start()
        self._target_room = getattr(self._target, "target_room", "")
        should_forward = self._state.get_forwarding_enabled() if self._state else True
        if self._state:
            self._forwarding_paused = self._state.get_forwarding_paused()
        if should_forward:
            logger.info("BridgeCore starting source backend")
            self._forwarding_enabled = True
            await self._source.start()
            self._source_started = True
            if self._forwarding_paused:
                logger.log(ALWAYS, "Source and target ready, forwarding is paused. Send !resume to resume.")
            else:
                logger.log(ALWAYS, "Source and target backends ready, forwarding active")
        else:
            logger.log(ALWAYS, "Forwarding was disabled on last run; source not started. Send !login to connect.")

    async def run(self) -> None:
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        self._shutdown_event.set()
        logger.log(ALWAYS, "BridgeCore stopping")
        backends = []
        if self._target:
            backends.append(self._target.stop())
        if self._source_started:
            backends.append(self._source.stop())
        await asyncio.gather(*backends, return_exceptions=True)

    async def _store_message(self, msg: BridgeMessage) -> None:
        if self._store:
            try:
                await asyncio.to_thread(self._store.save_message, msg, self._media_dir)
            except Exception:
                logger.error("Failed to save message to store", exc_info=True)
            try:
                if msg.sender_displayname and msg.sender_displayname != msg.sender:
                    await asyncio.to_thread(
                        self._store.upsert_user_alias,
                        msg.sender, msg.sender_displayname,
                    )
                if msg.source_room_name and msg.source_room_name != msg.source_room_id:
                    await asyncio.to_thread(
                        self._store.upsert_room_alias,
                        msg.source_room_id, msg.source_room_name,
                    )
            except Exception:
                pass

    async def _on_source_message(self, msg: BridgeMessage) -> None:
        if msg.direction == MessageDirection.REDACT:
            await self._on_source_redact(msg)
            return
        if msg.direction == MessageDirection.EDIT:
            await self._on_source_edit(msg)
            return
        if msg.direction != MessageDirection.FORWARD:
            return
        await self._store_message(msg)
        if msg.from_self:
            return
        if self._backup_mode:
            return
        if not self._forwarding_enabled or self._forwarding_paused:
            logger.debug("Forwarding paused, message saved but not forwarded from %s", msg.source_room_id)
            return
        target = self._target
        if not target:
            return
        target_room = self._target_room
        if not target_room:
            logger.warning("Target room not configured, dropping message")
            return
        try:
            if msg.msgtype == MessageType.CALL_NOTIFICATION:
                await self._forward_call_notification(target, target_room, msg)
            elif msg.msgtype in MEDIA_MSGTYPE_MAP and msg.media_data and self._media_enabled:
                await self._forward_media(target, target_room, msg)
            else:
                await self._forward_text(target, target_room, msg)
        except Exception as e:
            logger.error("Failed to forward message from %s: %s", msg.source_room_id, e)

    async def _save_event_map(self, event_id: str, room_id: str) -> None:
        self._room_id_map[event_id] = room_id
        if self._state:
            await self._state.save_event_room(event_id, room_id)

    def _lookup_event_room(self, event_id: str) -> Optional[str]:
        room_id = self._room_id_map.get(event_id)
        if not room_id and self._state:
            room_id = self._state.get_event_room(event_id)
            if room_id:
                self._room_id_map[event_id] = room_id
        return room_id

    async def _save_source_target(self, source_event_id: str, target_event_id: str) -> None:
        self._source_to_target_map[source_event_id] = target_event_id
        if self._state:
            await self._state.save_source_target(source_event_id, target_event_id)

    def _lookup_target_event(self, source_event_id: str) -> Optional[str]:
        target_id = self._source_to_target_map.get(source_event_id)
        if not target_id and self._state:
            target_id = self._state.get_target_event_id(source_event_id)
            if target_id:
                self._source_to_target_map[source_event_id] = target_id
        return target_id

    async def _forward_text(self, target: BaseBackend, target_room: str, msg: BridgeMessage) -> None:
        formatted = self._format_message(msg)
        event_id = await target.send_message(target_room, formatted, "m.notice")
        if event_id:
            await self._save_event_map(event_id, msg.source_room_id)
            await self._save_source_target(msg.event_id, event_id)
        logger.debug("Forwarded text: %s -> %s", msg.source_room_name, target_room)

    async def _forward_media(self, target: BaseBackend, target_room: str, msg: BridgeMessage) -> None:
        header = self._message_format.format(
            room_name=msg.source_room_name,
            sender=msg.sender_displayname,
            text="",
        ).rstrip(": ")
        nio_msgtype = MEDIA_MSGTYPE_MAP.get(msg.msgtype, "m.file")
        extra_info = {}
        if msg.media_width:
            extra_info["w"] = msg.media_width
        if msg.media_height:
            extra_info["h"] = msg.media_height
        if msg.media_duration:
            extra_info["duration"] = msg.media_duration
        event_id = await target.send_media(
            room_id=target_room,
            data=msg.media_data or b"",
            mimetype=msg.media_mimetype or "application/octet-stream",
            filename=msg.media_filename or "file",
            msgtype=nio_msgtype,
            extra_info=extra_info if extra_info else None,
        )
        if header and event_id:
            caption = f"{header} sent a {msg.msgtype.value.split('.')[-1]}: {msg.media_filename or 'file'}"
            await target.send_message(target_room, caption, "m.notice")
        if event_id:
            await self._save_event_map(event_id, msg.source_room_id)
            await self._save_source_target(msg.event_id, event_id)
        logger.debug("Forwarded media: %s -> %s", msg.source_room_name, target_room)

    async def _forward_call_notification(self, target: BaseBackend, target_room: str, msg: BridgeMessage) -> None:
        if not self._call_enabled:
            return
        call_type = msg.call_type or "unknown"
        sender = msg.sender_displayname
        if msg.call_action == CallAction.STARTED:
            text = f"📞 {sender} started a {call_type} call in [{msg.source_room_name}]"
        elif msg.call_action == CallAction.ANSWERED:
            text = f"📞 {sender} answered a {call_type} call in [{msg.source_room_name}]"
        elif msg.call_action == CallAction.ENDED:
            duration = ""
            if msg.call_duration:
                mins, secs = divmod(msg.call_duration, 60)
                duration = f" (duration: {mins}m {secs}s)"
            text = f"📞 {call_type} call ended in [{msg.source_room_name}]{duration}"
        else:
            text = f"📞 Call event in [{msg.source_room_name}] from {sender}"
        await target.send_message(target_room, text, "m.notice")
        logger.debug("Forwarded call notification: %s", text)

    async def _resolve_reply_room(self, event_id: str) -> Optional[str]:
        if not self._source_started or not self._target:
            return None
        target_room = self._target_room
        if not target_room:
            return None
        try:
            body = await self._target.get_event_body(target_room, event_id)
        except Exception:
            return None
        if not body or not body.startswith("["):
            if body and self._message_format != DEFAULT_MESSAGE_FORMAT:
                logger.warning(
                    "Cannot resolve reply room: message_format is '%s' which does not start with [room_name]. "
                    "Reply-to resolution requires message_format to begin with [{room_name}]. "
                    "Event %s will not be routed.",
                    self._message_format, event_id,
                )
            return None
        try:
            end = body.index("]")
        except ValueError:
            return None
        room_ref = body[1:end]
        resolved = await self._source.resolve_room_id(room_ref)
        if resolved:
            await self._save_event_map(event_id, resolved)
        return resolved

    async def _send_notice(self, room_id: str, text: str) -> None:
        if not self._target:
            return
        try:
            await self._target.send_message(room_id, text, "m.notice")
        except Exception:
            logger.error("Failed to send notice to %s", room_id)

    async def _handle_control(self, msg: BridgeMessage) -> None:
        if self._backup_mode:
            return
        target_room = self._target_room
        if not target_room or msg.source_room_id != target_room:
            logger.warning("Control command from unexpected room %s, ignoring", msg.source_room_id)
            return
        if self._admin_users and msg.sender not in self._admin_users:
            logger.warning("Control command from unauthorized sender %s, ignoring", msg.sender)
            return
        async with self._control_lock:
            await self._handle_control_locked(msg)

    async def _handle_control_locked(self, msg: BridgeMessage) -> None:
        action = msg.text
        if action == "login":
            if self._source_started:
                await self._send_notice(msg.source_room_id, "Source is already connected.")
                return
            try:
                await self._source.start()
                self._source_started = True
                self._forwarding_enabled = True
                self._forwarding_paused = False
                logger.log(ALWAYS, "Source backend started by %s", msg.sender)
                await self._send_notice(msg.source_room_id, "Source connected, forwarding resumed.")
            except Exception as e:
                logger.error("Failed to start source: %s", e)
                await self._send_notice(msg.source_room_id, f"Failed to connect source: {e}")
        elif action == "logout":
            if not self._source_started:
                await self._send_notice(msg.source_room_id, "Source is already disconnected.")
                return
            try:
                await self._source.stop()
                self._source_started = False
                self._forwarding_enabled = False
                self._forwarding_paused = False
                self._source_to_target_map.clear()
                self._room_id_map.clear()
                if self._state:
                    self._state.clear_mappings()
                logger.log(ALWAYS, "Source backend stopped by %s", msg.sender)
                await self._send_notice(msg.source_room_id, "Source disconnected, forwarding paused.")
            except Exception as e:
                logger.error("Failed to stop source: %s", e)
                await self._send_notice(msg.source_room_id, f"Failed to disconnect source: {e}")
        elif action == "status":
            lines = []
            lines.append(f"Source: {'connected' if self._source_started else 'disconnected'}")
            if self._source_started:
                if self._forwarding_paused:
                    lines.append("Forwarding: paused (messages are saved but not forwarded)")
                else:
                    lines.append("Forwarding: active")
            else:
                lines.append("Forwarding: disabled (source not connected)")
            await self._send_notice(msg.source_room_id, "\n".join(lines))
            return
        elif action == "pause":
            if not self._source_started:
                await self._send_notice(msg.source_room_id, "Source is not connected. Use !login first.")
                return
            if self._forwarding_paused:
                await self._send_notice(msg.source_room_id, "Forwarding is already paused.")
                return
            self._forwarding_paused = True
            logger.log(ALWAYS, "Forwarding paused by %s", msg.sender)
            await self._send_notice(msg.source_room_id, "Forwarding paused. Messages are still being saved but not forwarded to target. Use !resume to resume.")
        elif action == "resume":
            if not self._forwarding_paused:
                await self._send_notice(msg.source_room_id, "Forwarding is not paused.")
                return
            self._forwarding_paused = False
            logger.log(ALWAYS, "Forwarding resumed by %s", msg.sender)
            await self._send_notice(msg.source_room_id, "Forwarding resumed. Note: messages received during pause were saved but not forwarded.")
        else:
            return
        if self._state:
            await self._state.set_forwarding_enabled(self._forwarding_enabled)
            await self._state.set_forwarding_paused(self._forwarding_paused)
            await self._state.flush()

    async def _on_source_edit(self, msg: BridgeMessage) -> None:
        if not msg.edit_of_event_id:
            return
        if self._store:
            try:
                await asyncio.to_thread(self._store.update_message_text, msg.edit_of_event_id, msg.text)
            except Exception:
                logger.error("Failed to update message in store", exc_info=True)
        if msg.from_self or self._backup_mode:
            return
        if not self._forwarding_enabled or self._forwarding_paused:
            return
        target_event_id = self._lookup_target_event(msg.edit_of_event_id)
        if not target_event_id:
            logger.debug("Edited event %s not in map, skipping target edit", msg.edit_of_event_id)
            return
        if not self._target:
            return
        target_room = self._target_room
        if not target_room:
            return
        formatted = self._format_message(msg)
        try:
            await self._target.edit_message(target_room, target_event_id, formatted)
            logger.debug("Edit forwarded: %s -> %s", msg.edit_of_event_id, target_event_id)
        except Exception as e:
            logger.error("Failed to forward edit for %s: %s", msg.edit_of_event_id, e)

    async def _on_source_redact(self, msg: BridgeMessage) -> None:
        if msg.redacted_event_id and self._store:
            try:
                await asyncio.to_thread(self._store.delete_message, msg.redacted_event_id)
                logger.info("Redacted message deleted from store: %s", msg.redacted_event_id)
            except Exception:
                logger.error("Failed to delete redacted message from store", exc_info=True)
        if self._backup_mode or not self._target:
            return
        if not msg.redacted_event_id:
            return
        target_event_id = self._lookup_target_event(msg.redacted_event_id)
        if not target_event_id:
            logger.debug("Redacted event %s not in map, skipping", msg.redacted_event_id)
            return
        target_room = self._target_room
        if not target_room:
            return
        try:
            await self._target.redact_event(target_room, target_event_id)
            logger.info("Redaction forwarded: %s -> %s", msg.redacted_event_id, target_event_id)
            self._source_to_target_map.pop(msg.redacted_event_id, None)
            if self._state:
                self._state.pop_source_target(msg.redacted_event_id)
        except Exception as e:
            logger.error("Failed to forward redaction: %s", e)

    async def _forward_to_source(self, room_id: str, msg: BridgeMessage) -> None:
        if not self._source_started:
            await self._send_notice(msg.source_room_id, "Message delivery failed: source is disconnected, use !login first.")
            return
        if msg.msgtype in MEDIA_MSGTYPE_MAP and msg.media_data:
            await self._source.send_media(
                room_id=room_id,
                data=msg.media_data,
                mimetype=msg.media_mimetype or "application/octet-stream",
                filename=msg.media_filename or "file",
                msgtype=MEDIA_MSGTYPE_MAP[msg.msgtype],
            )
        else:
            await self._source.send_message(room_id, msg.text)
        if msg.event_id and msg.source_room_id and self._target and hasattr(self._target, "send_reaction"):
            try:
                await self._target.send_reaction(msg.source_room_id, msg.event_id)
            except Exception:
                pass

    async def _on_target_message(self, msg: BridgeMessage) -> None:
        if self._backup_mode:
            return
        if msg.direction == MessageDirection.CONTROL:
            await self._handle_control(msg)
            return
        if msg.direction != MessageDirection.REPLY:
            return
        if msg.reply_to_event_id:
            source_room_id = self._lookup_event_room(msg.reply_to_event_id)
            if not source_room_id:
                source_room_id = await self._resolve_reply_room(msg.reply_to_event_id)
            if source_room_id:
                msg.source_room_id = source_room_id
                msg.source_room_name = self._source.get_room_name_for(source_room_id)
                msg.sender = self._source.get_own_user_id()
                msg.sender_displayname = self._source.get_own_displayname()
                await self._store_message(msg)
                try:
                    await self._forward_to_source(source_room_id, msg)
                    logger.info("Reply forwarded (via reply-to): %s -> %s", msg.sender, source_room_id)
                except Exception as e:
                    logger.error("Failed to forward reply: %s", e)
                    await self._send_notice(msg.source_room_id, f"Message delivery failed: {e}")
                return
            logger.debug("Reply-to event %s not in bridge map, ignoring", msg.reply_to_event_id)
            return
        if not msg.target_room_id:
            return
        if not self._source_started:
            await self._send_notice(msg.source_room_id, "Message delivery failed: source is disconnected, use !login first.")
            return
        try:
            resolved = await self._source.resolve_room_id(msg.target_room_id)
            if not resolved:
                logger.warning("Cannot resolve room: %s", msg.target_room_id)
                await self._send_notice(msg.source_room_id, "Message delivery failed: could not resolve the target room.")
                return
            msg.source_room_id = resolved
            msg.source_room_name = self._source.get_room_name_for(resolved)
            msg.sender = self._source.get_own_user_id()
            msg.sender_displayname = self._source.get_own_displayname()
            await self._store_message(msg)
            await self._forward_to_source(resolved, msg)
            logger.info("Reply forwarded: %s -> %s", msg.sender, resolved)
        except Exception as e:
            logger.error("Failed to forward reply: %s", e)
            await self._send_notice(msg.source_room_id, f"Message delivery failed: {e}")
