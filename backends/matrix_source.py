from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from nio import (
    AsyncClient,
    CallAnswerEvent,
    CallHangupEvent,
    CallInviteEvent,
    Event,
    ForwardedRoomKeyEvent,
    KeyVerificationEvent,
    MatrixRoom,
    MegolmEvent,
    RedactionEvent,
    RoomEncryptedMedia,
    RoomKeyEvent,
    RoomMessage,
    RoomMessageEmote,
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageMedia,
    RoomMessageNotice,
    RoomMessageText,
    RoomMessageVideo,
    RoomMessageAudio,
    SyncResponse,
)

from backends.matrix_base import ALWAYS, MEDIA_MSGTYPES, MatrixBackend
from bridge.models import (
    BridgeMessage,
    CallAction,
    MessageDirection,
    MessageType,
)
from bridge.state import StateManager

logger = logging.getLogger(__name__)

CALL_CLEANUP_INTERVAL = 3600
CALL_MAX_AGE = 86400


class MatrixSourceBackend(MatrixBackend):
    def __init__(self, name: str, config: dict, state: StateManager, *, config_path: Optional[str] = None) -> None:
        super().__init__(name, config, state, config_path=config_path)
        self._active_calls: dict[str, dict] = {}
        self._call_cleanup_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        saved_token = await self._init_client()

        client = self._get_client()

        if saved_token:
            client.next_batch = saved_token
            logger.info("[%s] Resumed from sync token", self.name)

        resp = await client.sync(timeout=3000, full_state=True)
        if isinstance(resp, SyncResponse):
            logger.info("[%s] Initial sync done", self.name)
            if resp.next_batch:
                await self._state.save_sync_token(self.name, resp.next_batch)

        client.add_event_callback(
            self._on_room_event,
            (RoomMessage, CallInviteEvent, CallAnswerEvent, CallHangupEvent),
        )
        client.add_event_callback(self._on_encrypted_event, MegolmEvent)
        client.add_event_callback(self._on_redaction_event, RedactionEvent)
        self._register_common_callbacks(client)

        if client.should_query_keys:
            await client.keys_query()
            logger.info("[%s] Keys queried after initial sync", self.name)
        if client.should_claim_keys:
            users_to_claim = client.get_users_for_key_claiming()
            logger.info("[%s] Claiming keys for: %s", self.name, users_to_claim)
            await client.keys_claim(users_to_claim)
            logger.info("[%s] Keys claimed", self.name)

        await self._query_all_room_members()

        failed_sessions = self._state.get_failed_decryption_sessions()
        if failed_sessions:
            logger.info(
                "[%s] %d session(s) with failed decryptions from previous run, will retry when keys arrive",
                self.name, len(failed_sessions),
            )

        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush())
        self._key_upload_task = asyncio.create_task(self._periodic_key_upload())
        self._call_cleanup_task = asyncio.create_task(self._cleanup_stale_calls())
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.log(ALWAYS, "[%s] Started, beginning sync loop", self.name)

    async def _cleanup_stale_calls(self) -> None:
        while self._running:
            await asyncio.sleep(CALL_CLEANUP_INTERVAL)
            now = time.monotonic()
            stale = [
                cid for cid, info in self._active_calls.items()
                if now - info.get("monotonic_start", now) > CALL_MAX_AGE
            ]
            for cid in stale:
                del self._active_calls[cid]
                logger.debug("[%s] Cleaned up stale call: %s", self.name, cid)

    # -------------------------------------------------- key re-request hook

    async def _before_key_rerequest(self, client: AsyncClient, enc_event) -> None:
        try:
            await client.cancel_key_share(enc_event)
        except Exception:
            pass

    async def _on_pending_encrypted_enqueued(self, room, event, session_id: str, error) -> None:
        await self._state.save_failed_decryption(session_id, room.room_id, event.event_id)

    # -------------------------------------------------- room helpers

    async def _get_room_name(self, room: MatrixRoom) -> str:
        if room.name:
            return room.name
        if room.canonical_alias:
            return room.canonical_alias
        display = room.display_name or ""
        if display and display.lower().replace(" ", "") not in ("emptyroom", "empty"):
            return display
        if room.room_id in self._room_name_cache:
            return self._room_name_cache[room.room_id]
        client = self._get_client()
        try:
            resp = await client.room_get_state_event(room.room_id, "m.room.name", "")
            if hasattr(resp, "content"):
                name = resp.content.get("name")
                if name:
                    self._room_name_cache[room.room_id] = name
                    return name
        except Exception:
            pass
        try:
            resp = await client.room_get_state_event(room.room_id, "m.room.canonical_alias", "")
            if hasattr(resp, "content"):
                alias = resp.content.get("alias")
                if alias:
                    self._room_name_cache[room.room_id] = alias
                    return alias
                aliases = resp.content.get("alt_aliases")
                if aliases:
                    self._room_name_cache[room.room_id] = aliases[0]
                    return aliases[0]
        except Exception:
            pass
        return room.room_id

    async def _query_all_room_members(self) -> None:
        """Query device keys for all members of all joined encrypted rooms.

        Ensures matrix-nio has all members' device keys before the sync loop
        starts, so outgoing messages in encrypted rooms are sent correctly."""
        client = self._get_client()
        all_members: set[str] = set()
        encrypted_rooms = 0
        for room_id, room in client.rooms.items():
            if not room.encrypted:
                continue
            encrypted_rooms += 1
            all_members.update(room.users.keys())
        if not all_members:
            return
        try:
            for uid in all_members:
                client.users_for_key_query.add(uid)
            await client.keys_query()
            logger.info(
                "[%s] Queried device keys for %d members across %d encrypted room(s)",
                self.name, len(all_members), encrypted_rooms,
            )
        except Exception as e:
            logger.warning("[%s] Failed to query room member keys: %s", self.name, e)

    # -------------------------------------------------- event callbacks

    async def _on_room_event(self, room: MatrixRoom, event: Event) -> None:
        if self._state.is_processed(event.event_id):
            return
        await self._state.mark_processed(event.event_id)

        from_self = event.sender == self.config["user_id"]

        if isinstance(event, (CallInviteEvent, CallAnswerEvent, CallHangupEvent)):
            if from_self:
                return
            if isinstance(event, CallInviteEvent):
                await self._handle_call_invite(room, event)
            elif isinstance(event, CallAnswerEvent):
                await self._handle_call_answer(room, event)
            elif isinstance(event, CallHangupEvent):
                await self._handle_call_hangup(room, event)
        elif isinstance(event, RoomMessageText):
            await self._handle_text(room, event, MessageType.TEXT, from_self=from_self)
        elif isinstance(event, RoomMessageNotice):
            await self._handle_text(room, event, MessageType.NOTICE, from_self=from_self)
        elif isinstance(event, RoomMessageEmote):
            await self._handle_text(room, event, MessageType.EMOTE, from_self=from_self)
        elif isinstance(event, RoomMessageImage):
            await self._handle_media(room, event, "m.image", from_self=from_self)
        elif isinstance(event, RoomMessageVideo):
            await self._handle_media(room, event, "m.video", from_self=from_self)
        elif isinstance(event, RoomMessageAudio):
            await self._handle_media(room, event, "m.audio", from_self=from_self)
        elif isinstance(event, RoomMessageFile):
            await self._handle_media(room, event, "m.file", from_self=from_self)
        elif isinstance(event, RoomMessageMedia):
            msgtype = getattr(event, "msgtype", "m.file")
            await self._handle_media(room, event, msgtype, from_self=from_self)
        elif isinstance(event, RoomEncryptedMedia):
            msgtype = getattr(event, "msgtype", "m.file")
            await self._handle_media(room, event, msgtype, from_self=from_self)
        else:
            logger.debug("[%s] Unhandled event type: %s", self.name, type(event).__name__)

    async def _on_encrypted_event(self, room: MatrixRoom, event: MegolmEvent) -> None:
        if self._state.is_processed(event.event_id):
            return

        from_self = event.sender == self.config["user_id"]

        try:
            decrypted = await self._get_client().decrypt_event(event)
        except Exception as e:
            logger.warning("[%s] Failed to decrypt event %s: %s", self.name, event.event_id, e)
            await self._state.mark_processed(event.event_id)
            if not from_self:
                await self._enqueue_pending_encrypted(room, event, e)
            return

        if not isinstance(decrypted, RoomMessage):
            await self._state.mark_processed(event.event_id)
            return

        await self._state.mark_processed(event.event_id)
        await self._dispatch_decrypted(room, event.event_id, decrypted, from_self=from_self)

    async def _on_redaction_event(self, room: MatrixRoom, event: RedactionEvent) -> None:
        if event.sender == self.config["user_id"]:
            return
        if self._state.is_processed(event.event_id):
            return
        await self._state.mark_processed(event.event_id)
        msg = BridgeMessage(
            source_room_id=room.room_id,
            source_room_name=await self._get_room_name(room),
            sender=event.sender,
            sender_displayname=await self._get_sender_displayname(room, event.sender),
            text="",
            timestamp=event.server_timestamp,
            event_id=event.event_id,
            backend_name=self.name,
            direction=MessageDirection.REDACT,
            msgtype=MessageType.TEXT,
            redacted_event_id=event.redacts,
        )
        await self._emit_message(msg)

    async def _on_room_key_received(self, event) -> None:
        session_id = getattr(event, "session_id", None)
        if not session_id:
            return
        client = self._get_client()
        handled_ids: set[str] = set()

        # Retry in-memory pending events
        if session_id in self._pending_encrypted:
            entry = self._pending_encrypted.pop(session_id)
            pending = entry["events"]
            logger.info(
                "[%s] Room key arrived for session %s, retrying %d in-memory event(s)",
                self.name, session_id, len(pending),
            )
            for room, enc_event in pending:
                handled_ids.add(enc_event.event_id)
                try:
                    decrypted = await client.decrypt_event(enc_event)
                    await self._dispatch_decrypted(room, enc_event.event_id, decrypted)
                except Exception as e:
                    logger.warning("[%s] Retry decrypt failed for %s: %s", self.name, enc_event.event_id, e)

        # Retry persisted events (cross-restart scenario)
        persisted = self._state.pop_failed_decryptions(session_id)
        persisted = [p for p in persisted if p["event_id"] not in handled_ids]
        if persisted:
            logger.info(
                "[%s] Retrying %d persisted event(s) for session %s",
                self.name, len(persisted), session_id,
            )
        for item in persisted:
            try:
                resp = await client.room_get_event(item["room_id"], item["event_id"])
                if not hasattr(resp, "event"):
                    continue
                decrypted = await client.decrypt_event(resp.event)
                room = client.rooms.get(item["room_id"])
                if room:
                    await self._dispatch_decrypted(room, item["event_id"], decrypted)
                    logger.info("[%s] Persisted event %s decrypted after key arrival", self.name, item["event_id"])
            except Exception as e:
                logger.warning("[%s] Retry persisted event %s failed: %s", self.name, item["event_id"], e)

    # -------------------------------------------------- message handlers

    async def _handle_text(
        self,
        room: MatrixRoom,
        event: RoomMessage,
        msgtype: MessageType,
        original_event_id: Optional[str] = None,
        from_self: bool = False,
    ) -> None:
        source = getattr(event, "source", {})
        content = source.get("content", {}) if isinstance(source, dict) else {}
        relates_to = content.get("m.relates_to", {})
        new_content = content.get("m.new_content")

        if relates_to.get("rel_type") == "m.replace" and new_content and isinstance(new_content, dict):
            edited_text = new_content.get("body", "")
            if not edited_text:
                edited_text = (event.body or "").lstrip("* ")
            msg = BridgeMessage(
                source_room_id=room.room_id,
                source_room_name=await self._get_room_name(room),
                sender=event.sender,
                sender_displayname=await self._get_sender_displayname(room, event.sender),
                text=edited_text,
                timestamp=event.server_timestamp,
                event_id=original_event_id or event.event_id,
                backend_name=self.name,
                direction=MessageDirection.EDIT,
                msgtype=msgtype,
                edit_of_event_id=relates_to.get("event_id", ""),
                from_self=from_self,
            )
            await self._emit_message(msg)
            return

        msg = BridgeMessage(
            source_room_id=room.room_id,
            source_room_name=await self._get_room_name(room),
            sender=event.sender,
            sender_displayname=await self._get_sender_displayname(room, event.sender),
            text=event.body or "",
            timestamp=event.server_timestamp,
            event_id=original_event_id or event.event_id,
            backend_name=self.name,
            direction=MessageDirection.FORWARD,
            msgtype=msgtype,
            from_self=from_self,
        )
        await self._emit_message(msg)

    async def _handle_media(
        self,
        room: MatrixRoom,
        event: RoomMessage,
        msgtype_str: str,
        original_event_id: Optional[str] = None,
        from_self: bool = False,
    ) -> None:
        msgtype = MEDIA_MSGTYPES.get(msgtype_str, MessageType.FILE)
        media_url, info, data = await self._download_media(event)
        msg = BridgeMessage(
            source_room_id=room.room_id,
            source_room_name=await self._get_room_name(room),
            sender=event.sender,
            sender_displayname=await self._get_sender_displayname(room, event.sender),
            text=event.body or "",
            timestamp=event.server_timestamp,
            event_id=original_event_id or event.event_id,
            backend_name=self.name,
            direction=MessageDirection.FORWARD,
            msgtype=msgtype,
            media_url=media_url,
            media_data=data,
            media_mimetype=info.get("mimetype", "application/octet-stream"),
            media_filename=getattr(event, "body", "file"),
            media_size=info.get("size", len(data) if data else 0),
            thumbnail_url=info.get("thumbnail_url"),
            media_width=info.get("w"),
            media_height=info.get("h"),
            media_duration=info.get("duration"),
            from_self=from_self,
        )
        await self._emit_message(msg)

    async def _dispatch_decrypted(self, room: MatrixRoom, original_event_id: str, decrypted, from_self: bool = False) -> None:
        if not isinstance(decrypted, RoomMessage):
            return
        await self._state.mark_processed(original_event_id)
        if isinstance(decrypted, RoomMessageText):
            await self._handle_text(room, decrypted, MessageType.TEXT, original_event_id=original_event_id, from_self=from_self)
        elif isinstance(decrypted, RoomMessageNotice):
            await self._handle_text(room, decrypted, MessageType.NOTICE, original_event_id=original_event_id, from_self=from_self)
        elif isinstance(decrypted, (RoomMessageMedia, RoomEncryptedMedia)):
            msgtype = getattr(decrypted, "msgtype", "m.file")
            await self._handle_media(room, decrypted, msgtype, original_event_id=original_event_id, from_self=from_self)
        else:
            await self._handle_text(room, decrypted, MessageType.TEXT, original_event_id=original_event_id, from_self=from_self)

    # -------------------------------------------------- call handlers

    async def _handle_call_invite(self, room: MatrixRoom, event: CallInviteEvent) -> None:
        call_id = getattr(event, "call_id", "unknown")
        is_video = False
        invite_content = getattr(event, "source", {})
        if isinstance(invite_content, dict):
            offer = invite_content.get("content", {}).get("offer", {})
            sdp = offer.get("sdp", "") if isinstance(offer, dict) else ""
            is_video = "video" in sdp.lower() if sdp else False

        self._active_calls[call_id] = {
            "room_id": room.room_id,
            "sender": event.sender,
            "is_video": is_video,
            "call_id": call_id,
            "start_time": event.server_timestamp,
            "monotonic_start": time.monotonic(),
        }

        call_type = "video" if is_video else "voice"
        msg = BridgeMessage(
            source_room_id=room.room_id,
            source_room_name=await self._get_room_name(room),
            sender=event.sender,
            sender_displayname=await self._get_sender_displayname(room, event.sender),
            text=f"📞 {call_type} call started",
            timestamp=event.server_timestamp,
            event_id=event.event_id,
            backend_name=self.name,
            direction=MessageDirection.FORWARD,
            msgtype=MessageType.CALL_NOTIFICATION,
            call_type=call_type,
            call_action=CallAction.STARTED,
        )
        await self._emit_message(msg)

    async def _handle_call_answer(self, room: MatrixRoom, event: CallAnswerEvent) -> None:
        call_id = getattr(event, "call_id", "unknown")
        call_info = self._active_calls.get(call_id)
        call_type = "video" if call_info and call_info.get("is_video") else "voice"
        msg = BridgeMessage(
            source_room_id=room.room_id,
            source_room_name=await self._get_room_name(room),
            sender=event.sender,
            sender_displayname=await self._get_sender_displayname(room, event.sender),
            text="📞 Call answered",
            timestamp=event.server_timestamp,
            event_id=event.event_id,
            backend_name=self.name,
            direction=MessageDirection.FORWARD,
            msgtype=MessageType.CALL_NOTIFICATION,
            call_type=call_type,
            call_action=CallAction.ANSWERED,
        )
        await self._emit_message(msg)

    async def _handle_call_hangup(self, room: MatrixRoom, event: CallHangupEvent) -> None:
        call_id = getattr(event, "call_id", "unknown")
        call_info = self._active_calls.pop(call_id, None)

        if call_info:
            call_type = "video" if call_info.get("is_video") else "voice"
            duration_ms = event.server_timestamp - call_info.get("start_time", event.server_timestamp)
            call_duration = max(0, duration_ms // 1000)
        else:
            call_type = "unknown"
            call_duration = None

        msg = BridgeMessage(
            source_room_id=room.room_id,
            source_room_name=await self._get_room_name(room),
            sender=event.sender,
            sender_displayname=await self._get_sender_displayname(room, event.sender),
            text=f"📞 {call_type} call ended",
            timestamp=event.server_timestamp,
            event_id=event.event_id,
            backend_name=self.name,
            direction=MessageDirection.FORWARD,
            msgtype=MessageType.CALL_NOTIFICATION,
            call_type=call_type,
            call_action=CallAction.ENDED,
            call_duration=call_duration,
        )
        await self._emit_message(msg)

