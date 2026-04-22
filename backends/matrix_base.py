from __future__ import annotations

import asyncio
import getpass
import logging
import os
import stat
import time
from io import BytesIO
from typing import Optional

import yaml

from nio import (
    AsyncClient,
    AsyncClientConfig,
    DownloadError,
    ForwardedRoomKeyEvent,
    KeyVerificationEvent,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    RoomEncryptedMedia,
    RoomKeyEvent,
    RoomMessageMedia,
    SyncResponse,
    ToDeviceError,
    ToDeviceMessage,
    UnknownToDeviceEvent,
    WhoamiResponse,
)

from backends.base import BaseBackend
from bridge.models import MessageType
from bridge.state import StateManager

ALWAYS = 60

logger = logging.getLogger(__name__)

MEDIA_MSGTYPES: dict[str, MessageType] = {
    "m.image": MessageType.IMAGE,
    "m.video": MessageType.VIDEO,
    "m.audio": MessageType.AUDIO,
    "m.file": MessageType.FILE,
}

SYNC_TIMEOUT = 30000
FLUSH_INTERVAL = 60
KEY_RECHECK_INTERVAL = 120
MAX_PENDING_SESSIONS = 200


class MatrixBackend(BaseBackend):
    """Shared Matrix client logic for source and target backends."""

    def __init__(self, name: str, config: dict, state: StateManager, *, config_path: Optional[str] = None) -> None:
        super().__init__(name, config)
        self._state = state
        self._client: Optional[AsyncClient] = None
        self._running = False
        self._flush_task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._key_upload_task: Optional[asyncio.Task] = None
        self._call_cleanup_task: Optional[asyncio.Task] = None
        self._pending_encrypted: dict[str, dict] = {}
        self._config_path = config_path

    # ------------------------------------------------------------------ client

    def _get_client(self) -> AsyncClient:
        assert self._client is not None
        return self._client

    def _get_sender_displayname(self, room, sender: str) -> str:
        user = room.users.get(sender)
        if user and user.display_name:
            return user.display_name
        return sender

    def get_own_user_id(self) -> str:
        return self.config.get("user_id", "")

    def get_own_displayname(self) -> str:
        client = self._client
        if not client:
            return self.config.get("user_id", "")
        for room in client.rooms.values():
            user = room.users.get(client.user_id)
            if user and user.display_name:
                return user.display_name
        return client.user_id

    def get_room_name_for(self, room_id: str) -> str:
        client = self._client
        if not client:
            return room_id
        room = client.rooms.get(room_id)
        if not room:
            return room_id
        if room.name:
            return room.name
        if room.canonical_alias:
            return room.canonical_alias
        display = room.display_name or ""
        if display and display.lower().replace(" ", "") not in ("emptyroom", "empty"):
            return display
        return room_id

    async def _init_client(self) -> Optional[str]:
        """Create, authenticate, and prepare the Matrix client.

        Returns the saved sync token (if any) so the caller can restore it
        after registering callbacks and performing an initial full-state sync.
        On first login (no device_id in config) the server assigns a new
        device_id, which is then written back to the config file.
        """
        store_path = os.path.abspath(self.config.get("store_path", f"./store/{self.name}"))
        os.makedirs(store_path, exist_ok=True)

        client_config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=self.config.get("handle_encrypted", True),
        )
        # Use stored device_id when available; "" means "let server assign a new one".
        self._client = AsyncClient(
            homeserver=self.config["homeserver"],
            user=self.config["user_id"],
            device_id=self.config.get("device_id") or "",
            store_path=store_path,
            config=client_config,
        )

        access_token = self.config.get("access_token", "")
        if access_token:
            access_token = access_token.strip()
            self._client.restore_login(
                user_id=self.config["user_id"],
                device_id=self.config.get("device_id") or "",
                access_token=access_token,
            )
            logger.log(ALWAYS, "[%s] Restored login with access_token", self.name)
        else:
            password = self.config.get("password", "")
            if not password:
                password = getpass.getpass(f"[{self.name}] Password for {self.config['user_id']}: ")
            if not password:
                raise RuntimeError(f"No password provided for {self.name}")
            resp = await self._client.login(password)
            if hasattr(resp, "access_token"):
                self._client.access_token = resp.access_token
                assigned_device_id = getattr(resp, "device_id", None)
                if assigned_device_id and not self.config.get("device_id"):
                    self._client.device_id = assigned_device_id
                    self.config["device_id"] = assigned_device_id
                    logger.info("[%s] Server assigned device_id: %s", self.name, assigned_device_id)
                    await self._persist_device_id()
                logger.info("[%s] Logged in successfully", self.name)
            else:
                logger.error("[%s] Login failed: %s", self.name, resp)
                raise RuntimeError(f"Login failed for {self.name}: {resp}")

        # Upload unconditionally: should_upload_keys may be stale when the
        # homeserver omits one_time_key_counts from the sync response.
        try:
            await self._client.keys_upload()
        except Exception:
            pass
        if self._client.should_query_keys:
            await self._client.keys_query()

        await self._import_keys_if_configured()
        saved_token = self._state.load_sync_token(self.name)
        await self._verify_connection()
        return saved_token

    async def _persist_device_id(self) -> None:
        """Write the newly assigned device_id back to the config file.

        Reads the existing YAML (which keeps encrypted tokens intact), patches
        only the device_id field for this backend's section, and writes it back
        atomically.  The original file permissions are preserved.
        """
        device_id = self.config.get("device_id")
        if not device_id:
            return
        if not self._config_path:
            logger.info(
                "[%s] config_path not set — add 'device_id: %s' to the [%s] section manually.",
                self.name, device_id, self.name,
            )
            return
        try:
            with open(self._config_path, "r") as f:
                file_config = yaml.safe_load(f) or {}
            section = file_config.get(self.name, {})
            section["device_id"] = device_id
            file_config[self.name] = section
            tmp_path = self._config_path + ".tmp"
            with open(tmp_path, "w") as f:
                yaml.dump(file_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            os.replace(tmp_path, self._config_path)
            os.chmod(self._config_path, stat.S_IRUSR | stat.S_IWUSR)
            logger.info("[%s] device_id saved to %s", self.name, self._config_path)
        except Exception as e:
            logger.error("[%s] Failed to persist device_id: %s", self.name, e)

    async def _import_keys_if_configured(self) -> None:
        key_file = self.config.get("key_import_file", "")
        key_passphrase = self.config.get("key_import_passphrase", "")
        if not key_file or not key_passphrase:
            return
        key_file = os.path.abspath(key_file)
        if not os.path.isfile(key_file):
            logger.warning("[%s] Key import file not found: %s", self.name, key_file)
            return
        client = self._get_client()
        try:
            await client.import_keys(key_file, key_passphrase)
            logger.info("[%s] Imported encryption keys from %s", self.name, key_file)
            self.config["key_import_file"] = ""
            self.config["key_import_passphrase"] = ""
            logger.info("[%s] key_import_file and key_import_passphrase can now be removed from config", self.name)
        except Exception as e:
            logger.error("[%s] Failed to import keys: %s", self.name, e)

    async def _verify_connection(self) -> None:
        client = self._get_client()
        resp = await client.whoami()
        if isinstance(resp, WhoamiResponse):
            logger.log(ALWAYS, "[%s] Auth verified: user=%s device=%s", self.name, resp.user_id, resp.device_id)
        else:
            logger.log(ALWAYS, "[%s] Auth check failed: %s — check access_token and homeserver", self.name, resp)
            raise RuntimeError(f"Auth check failed for {self.name}: {resp}")

    # ---------------------------------------------------------------- lifecycle

    async def stop(self) -> None:
        self._running = False
        for attr in ("_flush_task", "_key_upload_task", "_sync_task", "_call_cleanup_task"):
            task = getattr(self, attr)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        if self._client:
            await self._state.save_sync_token(self.name, self._client.next_batch)
            await self._state.flush()
            await self._client.close()
            self._client = None
            logger.log(ALWAYS, "[%s] Stopped", self.name)

    # -------------------------------------------------------- background tasks

    async def _periodic_flush(self) -> None:
        while self._running:
            await asyncio.sleep(FLUSH_INTERVAL)
            try:
                if self._client and self._client.next_batch:
                    await self._state.save_sync_token(self.name, self._client.next_batch)
                await self._state.flush()
            except Exception as e:
                logger.warning("[%s] Flush error: %s", self.name, e)

    async def _periodic_key_upload(self) -> None:
        await asyncio.sleep(KEY_RECHECK_INTERVAL)
        while self._running:
            try:
                await self._get_client().keys_upload()
            except Exception as e:
                if "no key upload needed" not in str(e).lower():
                    logger.warning("[%s] Periodic key upload error: %s", self.name, e)
            try:
                await self._recheck_pending_keys()
            except Exception as e:
                logger.warning("[%s] Recheck pending keys error: %s", self.name, e)
            await asyncio.sleep(KEY_RECHECK_INTERVAL)

    async def _before_key_rerequest(self, client: AsyncClient, enc_event) -> None:
        """Hook called before each re-request. Source overrides to add cancel_key_share."""

    async def _recheck_pending_keys(self) -> None:
        client = self._get_client()
        now = time.monotonic()
        for session_id, entry in list(self._pending_encrypted.items()):
            if now - entry["last_requested"] < KEY_RECHECK_INTERVAL:
                continue
            events = entry["events"]
            if not events:
                continue
            _, enc_event = events[0]
            sender = enc_event.sender
            await self._before_key_rerequest(client, enc_event)
            try:
                await client.request_room_key(enc_event)
                entry["last_requested"] = now
                age = int(now - entry["first_seen"])
                logger.info(
                    "[%s] Re-requested room key for session %s (pending %ds, sender %s)",
                    self.name, session_id, age, sender,
                )
            except Exception as e:
                logger.warning("[%s] Re-request failed for session %s: %s", self.name, session_id, e)
            try:
                devices = list(client.device_store.active_user_devices(sender))
                if devices:
                    await client.keys_claim({sender: [d.id for d in devices]})
            except Exception:
                pass

    async def _enqueue_pending_encrypted(self, room, event, error) -> None:
        session_id = getattr(event, "session_id", None)
        if not session_id:
            return
        now = time.monotonic()
        if session_id not in self._pending_encrypted and len(self._pending_encrypted) >= MAX_PENDING_SESSIONS:
            oldest = next(iter(self._pending_encrypted))
            del self._pending_encrypted[oldest]
            logger.warning("[%s] pending_encrypted full, evicted oldest session %s", self.name, oldest)
        entry = self._pending_encrypted.setdefault(session_id, {
            "events": [], "first_seen": now, "last_requested": 0.0,
        })
        if not any(e2.event_id == event.event_id for _, e2 in entry["events"]):
            entry["events"].append((room, event))

        await self._on_pending_encrypted_enqueued(room, event, session_id, error)

        client = self._get_client()
        need_olm_session = "no olm" in str(error).lower() or entry["last_requested"] == 0.0
        if need_olm_session:
            try:
                client.users_for_key_query.add(event.sender)
                await client.keys_query()
            except Exception:
                pass
            try:
                devices = list(client.device_store.active_user_devices(event.sender))
                if devices:
                    await client.keys_claim({event.sender: [d.id for d in devices]})
            except Exception:
                pass
        if now - entry["last_requested"] >= KEY_RECHECK_INTERVAL:
            try:
                await client.request_room_key(event)
                entry["last_requested"] = now
                logger.info("[%s] Requested missing room key for session %s", self.name, session_id)
            except Exception as req_err:
                if "already sent" not in str(req_err).lower():
                    logger.warning("[%s] Failed to request room key: %s", self.name, req_err)

    async def _on_pending_encrypted_enqueued(self, room, event, session_id: str, error) -> None:
        pass

    def _register_common_callbacks(self, client: AsyncClient) -> None:
        """Register to-device and room callbacks shared by both source and target."""
        client.add_to_device_callback(self._on_key_verification, KeyVerificationEvent)
        client.add_event_callback(self._on_key_verification_event, KeyVerificationEvent)
        client.add_to_device_callback(self._on_unknown_to_device, UnknownToDeviceEvent)
        client.add_to_device_callback(self._on_room_key_received, RoomKeyEvent)
        client.add_to_device_callback(self._on_room_key_received, ForwardedRoomKeyEvent)

    async def _on_room_key_received(self, event) -> None:
        """Default no-op; subclasses override to retry pending encrypted events."""

    async def _after_sync(self, client: AsyncClient, resp: SyncResponse) -> None:
        """Hook called after each successful sync. Target overrides to check undecrypted events."""

    async def _sync_loop(self) -> None:
        client = self._get_client()
        while self._running:
            try:
                resp = await client.sync(timeout=SYNC_TIMEOUT)
                if isinstance(resp, SyncResponse):
                    if resp.next_batch:
                        await self._state.save_sync_token(self.name, resp.next_batch)
                    await self._after_sync(client, resp)
                    try:
                        if client.outgoing_to_device_messages:
                            await client.send_to_device_messages()
                        if client.should_upload_keys:
                            await client.keys_upload()
                        if client.should_query_keys:
                            await client.keys_query()
                        if client.should_claim_keys:
                            users_to_claim = client.get_users_for_key_claiming()
                            logger.info("[%s] Claiming keys for: %s", self.name, users_to_claim)
                            await client.keys_claim(users_to_claim)
                    except Exception as e:
                        logger.warning("[%s] Key maintenance error: %s", self.name, e)
                else:
                    tr = getattr(resp, "transport_response", None)
                    if tr is not None:
                        status = getattr(tr, "status", None)
                        body = ""
                        try:
                            body = await tr.text()
                        except Exception:
                            body = "<unreadable>"
                        logger.warning(
                            "[%s] Sync error (HTTP %s): %s | body: %.500s",
                            self.name, status, resp, body,
                        )
                    else:
                        logger.warning("[%s] Sync error: %s", self.name, resp)
                    await asyncio.sleep(5)
            except Exception as e:
                logger.error("[%s] Sync exception: %s", self.name, e)
                await asyncio.sleep(10)

    # ------------------------------------------------------- media helpers

    async def _download_media(self, event) -> tuple[Optional[str], dict, Optional[bytes]]:
        """Download media attached to a room event.

        Returns ``(media_url, info_dict, data_bytes)``.  ``data_bytes`` is
        ``None`` when the download fails or the file exceeds the size limit.
        """
        media_url: Optional[str] = None
        if isinstance(event, RoomMessageMedia):
            media_url = event.url
        elif hasattr(event, "url"):
            media_url = event.url

        info: dict = {}
        if hasattr(event, "media_info") and isinstance(event.media_info, dict):
            info = event.media_info
        if not info:
            content = getattr(event, "source", {})
            if isinstance(content, dict):
                info = content.get("content", content).get("info", {})
                if not isinstance(info, dict):
                    info = {}

        data: Optional[bytes] = None
        max_size: int = self.config.get("media_max_size", 50 * 1024 * 1024)
        if media_url:
            try:
                resp = await self._get_client().download(mxc=media_url)
                if isinstance(resp, DownloadError):
                    logger.warning("[%s] Download error for %s: %s", self.name, media_url, resp)
                else:
                    data = resp.body
                    if len(data) > max_size:
                        logger.info(
                            "[%s] Media too large (%d bytes), skipping download",
                            self.name, len(data),
                        )
                        data = None
                    elif isinstance(event, RoomEncryptedMedia):
                        try:
                            from nio.crypto.attachments import decrypt_attachment
                            data = decrypt_attachment(
                                data,
                                key=event.key["k"],
                                hash=event.hashes.get("sha256", ""),
                                iv=event.iv,
                            )
                        except Exception as e:
                            logger.warning("[%s] Failed to decrypt media %s: %s", self.name, media_url, e)
                            data = None
            except Exception as e:
                logger.warning("[%s] Failed to download media %s: %s", self.name, media_url, e)

        return media_url, info, data

    # --------------------------------------------------- send / redact / resolve

    async def send_message(self, room_id: str, text: str, msgtype: str = "m.text") -> str:
        client = self._get_client()
        content = {"msgtype": msgtype, "body": text}
        resp = await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=True,
        )
        if hasattr(resp, "event_id"):
            return resp.event_id
        logger.warning("[%s] send_message unexpected response: %s", self.name, resp)
        return ""

    async def send_media(
        self,
        room_id: str,
        data: bytes,
        mimetype: str,
        filename: str,
        msgtype: str = "m.file",
        extra_info: Optional[dict] = None,
    ) -> str:
        client = self._get_client()
        resp = await client.upload(
            data_provider=BytesIO(data),
            content_type=mimetype,
            filename=filename,
            filesize=len(data),
        )
        if not hasattr(resp, "content_uri"):
            logger.warning("[%s] upload failed: %s", self.name, resp)
            return ""
        info = {"mimetype": mimetype, "size": len(data)}
        if extra_info:
            info.update(extra_info)
        content = {
            "msgtype": msgtype,
            "body": filename,
            "url": resp.content_uri,
            "info": info,
        }
        send_resp = await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=True,
        )
        return getattr(send_resp, "event_id", "")

    async def redact_event(self, room_id: str, event_id: str, reason: Optional[str] = None) -> str:
        client = self._get_client()
        resp = await client.room_redact(room_id, event_id, reason=reason)
        if hasattr(resp, "event_id"):
            return resp.event_id
        logger.warning("[%s] redact_event unexpected response: %s", self.name, resp)
        return ""

    async def edit_message(self, room_id: str, event_id: str, new_text: str, msgtype: str = "m.notice") -> str:
        client = self._get_client()
        content = {
            "msgtype": msgtype,
            "body": f"* {new_text}",
            "m.new_content": {
                "msgtype": msgtype,
                "body": new_text,
            },
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": event_id,
            },
        }
        resp = await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=True,
        )
        if hasattr(resp, "event_id"):
            return resp.event_id
        logger.warning("[%s] edit_message unexpected response: %s", self.name, resp)
        return ""

    async def resolve_room_id(self, room_alias_or_id: str) -> Optional[str]:
        client = self._get_client()
        if room_alias_or_id.startswith("!"):
            return room_alias_or_id
        resp = await client.room_resolve_alias(room_alias_or_id)
        if hasattr(resp, "room_id"):
            return resp.room_id
        logger.warning("[%s] Cannot resolve alias %s: %s", self.name, room_alias_or_id, resp)
        return None

    # -------------------------------------------------- key verification

    async def _on_key_verification_event(self, room, event: KeyVerificationEvent) -> None:
        await self._on_key_verification(event)

    async def _on_key_verification(self, event: KeyVerificationEvent) -> None:
        client = self._get_client()
        if isinstance(event, KeyVerificationStart):
            logger.info("[%s] Key verification started by %s", self.name, event.sender)
            try:
                resp = await client.accept_key_verification(event.transaction_id)
                if isinstance(resp, ToDeviceError):
                    logger.error("[%s] Failed to accept key verification: %s", self.name, resp)
            except Exception as e:
                logger.warning("[%s] Cannot accept key verification from %s: %s", self.name, event.sender, e)
        elif isinstance(event, KeyVerificationKey):
            # Flush nio's internal queue first (contains our share_key() message)
            # so the other device receives our SAS public key before the MAC.
            if client.outgoing_to_device_messages:
                await client.send_to_device_messages()
            try:
                resp = await client.confirm_short_auth_string(event.transaction_id)
                if isinstance(resp, ToDeviceError):
                    logger.error("[%s] Failed to confirm key verification: %s", self.name, resp)
            except Exception as e:
                logger.warning("[%s] Cannot confirm SAS from %s: %s", self.name, event.sender, e)
        elif isinstance(event, KeyVerificationMac):
            sas = client.key_verifications.get(event.transaction_id)
            if sas and sas.verified:
                logger.info("[%s] Key verification completed with %s", self.name, event.sender)
                resp = await client.to_device(
                    ToDeviceMessage(
                        type="m.key.verification.done",
                        recipient=event.sender,
                        recipient_device=sas.other_olm_device.id,
                        content={"transaction_id": event.transaction_id},
                    )
                )
                if isinstance(resp, ToDeviceError):
                    logger.warning("[%s] Failed to send verification done: %s", self.name, resp)
            else:
                logger.warning("[%s] Key verification failed or canceled with %s", self.name, event.sender)

    async def _on_unknown_to_device(self, event: UnknownToDeviceEvent) -> None:
        if event.type == "m.key.verification.request":
            await self._handle_verification_request(event)
        # elif event.type == "...":
        #     await self._handle_other(event)

    async def _handle_verification_request(self, event: UnknownToDeviceEvent) -> None:
        content = event.source.get("content", {})
        transaction_id = content.get("transaction_id")
        from_device = content.get("from_device")
        if not transaction_id or not from_device:
            logger.warning("[%s] Malformed verification request from %s", self.name, event.sender)
            return
        client = self._get_client()
        logger.info("[%s] Key verification request from %s, sending ready", self.name, event.sender)
        resp = await client.to_device(
            ToDeviceMessage(
                type="m.key.verification.ready",
                recipient=event.sender,
                recipient_device=from_device,
                content={
                    "from_device": client.device_id,
                    "methods": ["m.sas.v1"],
                    "transaction_id": transaction_id,
                },
            )
        )
        if isinstance(resp, ToDeviceError):
            logger.error("[%s] Failed to send verification ready: %s", self.name, resp)
