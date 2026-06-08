from __future__ import annotations

import asyncio
import logging
import re
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
    ReceiptEvent,
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

        own_device_id = client.device_id or ""
        own_ed25519 = client.olm_account_identity_keys.get("ed25519", "?") if hasattr(client, "olm_account_identity_keys") else "?"
        own_curve25519 = client.olm_account_identity_keys.get("curve25519", "?") if hasattr(client, "olm_account_identity_keys") else "?"
        logger.log(ALWAYS,
            "[%s] Bridge device: id=%s ed25519=%s curve25519=%s",
            self.name, own_device_id, own_ed25519[:16] + "...", own_curve25519[:16] + "...",
        )
        logger.info(
            "[%s] Verify in Element that these keys match Settings → Security → Sessions → %s",
            self.name, own_device_id,
        )

        client.add_event_callback(
            self._on_room_event,
            (RoomMessage, CallInviteEvent, CallAnswerEvent, CallHangupEvent),
        )
        client.add_event_callback(self._on_encrypted_event, MegolmEvent)
        client.add_event_callback(self._on_redaction_event, RedactionEvent)
        client.add_ephemeral_callback(self._on_receipt_event, ReceiptEvent)
        self._register_common_callbacks(client)

        resp = await client.sync(timeout=3000, full_state=True)
        if isinstance(resp, SyncResponse):
            logger.info("[%s] Initial sync done – %d room(s)", self.name, len(client.rooms))
            if resp.next_batch:
                await self._state.save_sync_token(self.name, resp.next_batch)

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

        if self.config.get("key_import_file"):
            await self.import_key_file()
        if self.config.get("recovery_key"):
            n = await self.restore_key_backup()
            if n:
                logger.log(ALWAYS, "[%s] Key backup restored %d session(s) — retrying failed decryptions", self.name, n)
                retry_sessions = self._state.get_failed_decryption_sessions()
                if retry_sessions:
                    for sid in retry_sessions:
                        items = self._state.get_failed_decryption_events(sid)
                        for it in items:
                            try:
                                resp = await client.room_get_event(it["room_id"], it["event_id"])
                                if hasattr(resp, "event"):
                                    decrypted = await client.decrypt_event(resp.event)
                                    room = client.rooms.get(it["room_id"])
                                    if room:
                                        await self._dispatch_decrypted(room, it["event_id"], decrypted)
                                        await self._state.pop_failed_decryptions(sid)
                                        logger.info("[%s] Decrypted persisted event %s after key backup", self.name, it["event_id"])
                            except Exception:
                                pass
        else:
            encrypted_count = sum(1 for r in client.rooms.values() if r.encrypted)
            if encrypted_count:
                logger.log(
                    ALWAYS,
                    "[%s] No recovery_key configured — %d encrypted room(s) may have undecryptable messages. "
                    "Set source.recovery_key in config to restore keys from server backup.",
                    self.name, encrypted_count,
                )

        await self._ensure_olm_sessions()

        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush())
        self._key_upload_task = asyncio.create_task(self._periodic_key_upload())
        self._call_cleanup_task = asyncio.create_task(self._cleanup_stale_calls())
        self._token_refresh_task = asyncio.create_task(self._periodic_token_refresh())
        self._sync_task = asyncio.create_task(self._sync_loop())

        encrypted_rooms = sum(1 for r in client.rooms.values() if r.encrypted)
        megolm_sessions = 0
        try:
            megolm_sessions = len(client.olm_account_session_cache) if hasattr(client, "olm_account_session_cache") else 0
        except Exception:
            pass
        logger.log(
            ALWAYS,
            "[%s] Started: device=%s encrypted_rooms=%d megolm_sessions=%d pending_decrypt=%d",
            self.name, own_device_id, encrypted_rooms, megolm_sessions, len(self._pending_encrypted),
        )

    async def _ensure_olm_sessions(self) -> None:
        client = self._get_client()
        own_user_id = self.config.get("user_id", "")
        own_device_id = client.device_id
        all_devices: dict[str, list[str]] = {}
        encrypted_room_count = 0
        for room_id, room in client.rooms.items():
            if not room.encrypted:
                continue
            encrypted_room_count += 1
            for user_id in room.users.keys():
                if user_id in all_devices:
                    continue
                device_ids = [d.id for d in client.device_store.active_user_devices(user_id)]
                if device_ids:
                    all_devices[user_id] = device_ids
        own_other_devices = [
            d.id for d in client.device_store.active_user_devices(own_user_id)
            if d.id != own_device_id
        ]
        if own_other_devices:
            all_devices[own_user_id] = own_other_devices
        if not all_devices:
            return
        try:
            resp = await client.keys_claim(all_devices)
            claimed_count = sum(len(v) for v in all_devices.values())
            logger.info(
                "[%s] Proactively claimed one-time keys for %d device(s) across %d user(s) (incl. own)",
                self.name, claimed_count, len(all_devices),
            )
            if hasattr(resp, "one_time_keys") and resp.one_time_keys:
                for user_id, devices in resp.one_time_keys.items():
                    for device_id, key_data in devices.items():
                        logger.info("[%s] Got OTK for %s %s: %s", self.name, user_id, device_id, type(key_data).__name__)
        except Exception as e:
            logger.warning("[%s] Failed to claim keys for Olm sessions: %s", self.name, e)
            return
        pending = self._state.get_failed_decryption_sessions()
        if not pending:
            return
        logger.info(
            "[%s] Requesting keys for %d persisted failed session(s) across %d encrypted room(s)",
            self.name, len(pending), encrypted_room_count,
        )
        re_enqueued = 0
        for session_id in pending:
            items = self._state.get_failed_decryption_events(session_id)
            if not items:
                continue
            item = items[0]
            try:
                resp = await client.room_get_event(item["room_id"], item["event_id"])
                if not hasattr(resp, "event"):
                    continue
                enc_event = resp.event
                room = client.rooms.get(item["room_id"])
                if room and enc_event.event_id not in self._pending_event_ids:
                    now = time.monotonic()
                    entry = self._pending_encrypted.setdefault(session_id, {
                        "events": [], "first_seen": now, "last_requested": 0.0,
                    })
                    entry["events"].append((room, enc_event))
                    self._pending_event_ids.add(enc_event.event_id)
                    re_enqueued += 1
                if hasattr(enc_event, "session_id"):
                    try:
                        await client.request_room_key(enc_event)
                        logger.info(
                            "[%s] Requested key for session %s (sender %s, room %s)",
                            self.name, session_id, getattr(enc_event, "sender", "?"), item["room_id"],
                        )
                    except Exception as e:
                        if "already sent" not in str(e).lower():
                            logger.debug("[%s] Key request failed for %s: %s", self.name, session_id, e)
                    try:
                        await client.send_to_device_messages()
                    except Exception:
                        pass
            except Exception:
                pass
        if re_enqueued:
            logger.info("[%s] Re-enqueued %d event(s) into pending queue for periodic key retry", self.name, re_enqueued)

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
            err_str = str(e)
            session_id = getattr(event, "session_id", "?")
            sender_key = getattr(event, "sender_key", "?")
            device_id = getattr(event, "device_id", "?")
            logger.warning(
                "[%s] Failed to decrypt %s in %s: %s "
                "(sender=%s sender_key=%s device_id=%s session_id=%s)",
                self.name, event.event_id, room.room_id, err_str,
                event.sender, sender_key[:12] + "...", device_id, session_id[:12] + "...",
            )
            if "no session" in err_str.lower():
                sender_devices = list(self._get_client().device_store.active_user_devices(event.sender))
                logger.info(
                    "[%s] Sender %s has %d known device(s). "
                    "Bridge may not have received the megolm session key. "
                    "Verify this bridge device in Element to enable key sharing.",
                    self.name, event.sender, len(sender_devices),
                )
            await self._state.mark_processed(event.event_id)
            placeholder = BridgeMessage(
                source_room_id=room.room_id,
                source_room_name=await self._get_room_name(room),
                sender=event.sender,
                sender_displayname=await self._get_sender_displayname(room, event.sender),
                text="[Encrypted message \u2013 awaiting decryption key]",
                timestamp=event.server_timestamp,
                event_id=event.event_id,
                backend_name=self.name,
                direction=MessageDirection.FORWARD,
                msgtype=MessageType.TEXT,
                from_self=from_self,
            )
            await self._emit_message(placeholder)
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

    async def _on_receipt_event(self, room: MatrixRoom, event: ReceiptEvent) -> None:
        if self._read_receipt_callback is None:
            return
        own_user_id = self.config.get("user_id", "")
        for receipt in event.receipts:
            if receipt.user_id == own_user_id:
                continue
            rt = receipt.receipt_type.value if hasattr(receipt.receipt_type, "value") else receipt.receipt_type
            if rt != "m.read":
                continue
            await self._emit_read_receipt(receipt.event_id, room.room_id)

    async def _after_sync(self, client, resp):
        for room_id, room_info in resp.rooms.join.items():
            count = len(room_info.timeline.events)
            if count > 0:
                logger.info("[%s] sync: room %s got %d timeline event(s)", self.name, room_id, count)
            if not hasattr(self, "_known_room_ids"):
                self._known_room_ids: set[str] = set(client.rooms.keys())
            if room_id not in self._known_room_ids:
                self._known_room_ids.add(room_id)
                logger.info("[%s] New room detected: %s", self.name, room_id)
                if self._new_room_callback:
                    asyncio.create_task(self._new_room_callback(room_id))

        to_device = resp.to_device_events if hasattr(resp, "to_device_events") else []
        if to_device:
            for ev in to_device:
                ev_type = type(ev).__name__
                session_id = getattr(ev, "session_id", None)
                sender = getattr(ev, "sender", "?")
                if session_id:
                    logger.info(
                        "[%s] to-device: %s from %s session_id=%s",
                        self.name, ev_type, sender, session_id[:16] + "...",
                    )
                elif ev_type not in ("KeyVerificationEvent", "UnknownToDeviceEvent"):
                    logger.debug("[%s] to-device: %s from %s", self.name, ev_type, sender)

    async def _on_room_key_received(self, event) -> None:
        session_id = getattr(event, "session_id", None)
        if not session_id:
            return
        logger.info(
            "[%s] Room key received: session_id=%s, sender=%s, type=%s",
            self.name, session_id, getattr(event, "sender", "?"), type(event).__name__,
        )
        client = self._get_client()
        handled_ids: set[str] = set()

        # Retry in-memory pending events
        if session_id in self._pending_encrypted:
            entry = self._pending_encrypted.pop(session_id)
            pending = entry["events"]
            for _, ev in pending:
                self._pending_event_ids.discard(ev.event_id)
            logger.info(
                "[%s] Room key arrived for session %s, retrying %d in-memory event(s)",
                self.name, session_id, len(pending),
            )
            failed: list[tuple] = []
            for room, enc_event in pending:
                try:
                    decrypted = await client.decrypt_event(enc_event)
                    await self._dispatch_decrypted(room, enc_event.event_id, decrypted)
                    handled_ids.add(enc_event.event_id)
                except Exception as e:
                    logger.warning("[%s] Retry decrypt failed for %s: %s", self.name, enc_event.event_id, e)
                    failed.append((room, enc_event))
            if failed:
                new_entry = self._pending_encrypted.setdefault(session_id, {
                    "events": [], "first_seen": entry.get("first_seen", time.monotonic()),
                    "last_requested": entry.get("last_requested", 0.0),
                })
                for room, enc_event in failed:
                    if enc_event.event_id not in self._pending_event_ids:
                        self._pending_event_ids.add(enc_event.event_id)
                        new_entry["events"].append((room, enc_event))
                    await self._state.save_failed_decryption(session_id, room.room_id, enc_event.event_id)

        # Retry persisted events (cross-restart scenario)
        persisted = await self._state.pop_failed_decryptions(session_id)
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
                    handled_ids.add(item["event_id"])
                    logger.info("[%s] Persisted event %s decrypted after key arrival", self.name, item["event_id"])
            except Exception as e:
                logger.warning("[%s] Retry persisted event %s failed: %s", self.name, item["event_id"], e)
                await self._state.save_failed_decryption(session_id, item["room_id"], item["event_id"])

        remaining = len(self._pending_encrypted)
        remaining_persisted = sum(len(v) for v in self._failed_decryptions_snapshot())
        if remaining or remaining_persisted:
            logger.info(
                "[%s] After key for session %s: %d in-memory + %d persisted sessions still pending",
                self.name, session_id, remaining, remaining_persisted,
            )

    def _failed_decryptions_snapshot(self) -> dict[str, list[dict]]:
        return dict(self._state._failed_decryptions)

    # -------------------------------------------------- message handlers

    MENTION_START = "\x02"
    MENTION_END = "\x03"

    @staticmethod
    def _enrich_mentions(body: str, content: dict, displayname_map: dict | None = None) -> str:
        names_to_prefix: list[str] = []
        fmt = content.get("formatted_body", "")
        if fmt:
            for mxid, displayname in re.findall(
                r'<a\s+href="https?://matrix\.to/#/(@[^"]+)"[^>]*>([^<]+)</a>', fmt
            ):
                names_to_prefix.append(displayname)
        if not names_to_prefix:
            m_mentions = content.get("m.mentions")
            if isinstance(m_mentions, dict):
                for uid in m_mentions.get("user_ids", []):
                    dn = (displayname_map or {}).get(uid)
                    if dn and dn in body:
                        names_to_prefix.append(dn)
        if not names_to_prefix:
            return body
        ms = MatrixSourceBackend.MENTION_START
        me = MatrixSourceBackend.MENTION_END
        text = body
        for name in names_to_prefix:
            marker = f"{ms}{name}{me}"
            if marker not in text and name in text:
                text = text.replace(name, marker, 1)
        return text

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
        dn_map = {u_id: u.display_name for u_id, u in room.users.items() if u.display_name}

        if relates_to.get("rel_type") == "m.replace" and new_content and isinstance(new_content, dict):
            edited_text = new_content.get("body", "")
            if not edited_text:
                edited_text = (event.body or "").lstrip("* ")
            edited_text = self._enrich_mentions(edited_text, new_content, dn_map)
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
            text=self._enrich_mentions(event.body or "", content, dn_map),
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

