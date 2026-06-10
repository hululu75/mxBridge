from __future__ import annotations

import asyncio
import base64
import collections
import getpass
import logging
import os
import stat
import tempfile
import time
from io import BytesIO
from typing import Optional

import yaml

from nio import (
    AsyncClient,
    AsyncClientConfig,
    DownloadError,
    ForwardedRoomKeyEvent,
    InviteEvent,
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
from nio.crypto.attachments import decrypt_attachment

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
MAX_PENDING_SESSIONS = 1000
MAX_PENDING_EVENTS_PER_SESSION = 100
MAX_DISPLAYNAME_CACHE = 1000
MAX_ROOM_NAME_CACHE = 500


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
        self._token_refresh_task: Optional[asyncio.Task] = None
        self._token_obtained_at: float = 0.0
        self._matrix_refresh_unsupported: bool = False
        self._pending_encrypted: dict[str, dict] = {}
        self._pending_event_ids: set[str] = set()
        self._config_path = config_path
        self._displayname_cache: dict[str, str] = {}
        self._room_name_cache: dict[str, str] = {}
        self._displayname_order: collections.deque[str] = collections.deque()
        self._room_name_order: collections.deque[str] = collections.deque()
        self._backup_priv_bytes: Optional[bytes] = None
        self._backup_version: Optional[str] = None

    # ------------------------------------------------------------------ client

    def _get_client(self) -> AsyncClient:
        assert self._client is not None
        return self._client

    async def _get_sender_displayname(self, room, sender: str) -> str:
        user = room.users.get(sender)
        if user and user.display_name:
            return user.display_name
        if sender in self._displayname_cache:
            return self._displayname_cache[sender]
        try:
            resp = await self._get_client().get_displayname(sender)
            if hasattr(resp, "displayname") and resp.displayname:
                self._displayname_cache[sender] = resp.displayname
                if sender not in self._displayname_order:
                    self._displayname_order.append(sender)
                self._trim_cache(self._displayname_cache, self._displayname_order, MAX_DISPLAYNAME_CACHE)
                return resp.displayname
        except Exception:
            pass
        return sender

    def get_own_user_id(self) -> str:
        return self.config.get("user_id", "")

    def get_own_displayname(self) -> str:
        client = self._client
        if not client:
            return self.config.get("user_id", "")
        cached = self._displayname_cache.get("__own__")
        if cached:
            return cached
        for room in client.rooms.values():
            user = room.users.get(client.user_id)
            if user and user.display_name:
                self._displayname_cache["__own__"] = user.display_name
                if "__own__" not in self._displayname_order:
                    self._displayname_order.append("__own__")
                self._trim_cache(self._displayname_cache, self._displayname_order, MAX_DISPLAYNAME_CACHE)
                return user.display_name
        return client.user_id

    def _invalidate_own_displayname_cache(self) -> None:
        self._displayname_cache.pop("__own__", None)
        if "__own__" in self._displayname_order:
            self._displayname_order.remove("__own__")

    def _trim_cache(self, cache: dict, order: collections.deque, max_size: int) -> None:
        while len(cache) > max_size:
            if not order:
                break
            oldest = order.popleft()
            cache.pop(oldest, None)

    async def get_room_name_for(self, room_id: str) -> str:
        client = self._client
        if not client:
            return room_id
        room = client.rooms.get(room_id)
        if room:
            if room.name:
                return room.name
            if room.canonical_alias:
                return room.canonical_alias
            display = room.display_name or ""
            if display and display.lower().replace(" ", "") not in ("emptyroom", "empty"):
                return display
        if room_id in self._room_name_cache:
            return self._room_name_cache[room_id]
        try:
            resp = await client.room_get_state_event(room_id, "m.room.name", "")
            if hasattr(resp, "content"):
                name = resp.content.get("name")
                if name:
                    self._room_name_cache[room_id] = name
                    if room_id not in self._room_name_order:
                        self._room_name_order.append(room_id)
                    self._trim_cache(self._room_name_cache, self._room_name_order, MAX_ROOM_NAME_CACHE)
                    return name
        except Exception:
            pass
        try:
            resp = await client.room_get_state_event(room_id, "m.room.canonical_alias", "")
            if hasattr(resp, "content"):
                alias = resp.content.get("alias")
                if alias:
                    self._room_name_cache[room_id] = alias
                    if room_id not in self._room_name_order:
                        self._room_name_order.append(room_id)
                    self._trim_cache(self._room_name_cache, self._room_name_order, MAX_ROOM_NAME_CACHE)
                    return alias
                aliases = resp.content.get("alt_aliases")
                if aliases:
                    self._room_name_cache[room_id] = aliases[0]
                    if room_id not in self._room_name_order:
                        self._room_name_order.append(room_id)
                    self._trim_cache(self._room_name_cache, self._room_name_order, MAX_ROOM_NAME_CACHE)
                    return aliases[0]
        except Exception:
            pass
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
            device_id_val = self.config.get("device_id") or ""
            if not device_id_val:
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"{self.config['homeserver']}/_matrix/client/v3/account/whoami",
                            headers={"Authorization": f"Bearer {access_token}"},
                        ) as resp:
                            if resp.status == 200:
                                body = await resp.json()
                                device_id_val = body.get("device_id", "")
                                if device_id_val:
                                    self.config["device_id"] = device_id_val
                                    logger.info("[%s] Discovered device_id via whoami: %s", self.name, device_id_val)
                            else:
                                text = await resp.text()
                                logger.info("[%s] whoami returned %d: %s", self.name, resp.status, text[:200])
                except Exception as e:
                    logger.info("[%s] whoami for device_id failed: %s", self.name, e)
            if not device_id_val:
                logger.info("[%s] access_token present but no device_id, falling back to SSO", self.name)
                access_token = ""
            else:
                self._client.restore_login(
                    user_id=self.config["user_id"],
                    device_id=device_id_val,
                    access_token=access_token,
                )
                logger.log(ALWAYS, "[%s] Restored login with access_token (device=%s)", self.name, device_id_val)
                self._token_obtained_at = time.monotonic()
        if not access_token:
            element_url = self.config.get("element_url", "")
            if element_url:
                logger.info("[%s] Using SSO login...", self.name)
                try:
                    from backends.sso_login import sso_login
                    token, dev_id, refresh_tok = await sso_login(
                        homeserver=self.config["homeserver"],
                        element_url=element_url,
                        user_id=self.config["user_id"],
                        device_id=self.config.get("device_id") or "",
                        username=self.config.get("sso_username", ""),
                        password=self.config.get("password", ""),
                        recovery_key=self.config.get("recovery_key", ""),
                    )
                    if refresh_tok:
                        self.config["refresh_token"] = refresh_tok
                    old_device_id = self.config.get("device_id") or ""
                    if dev_id and dev_id != old_device_id:
                        logger.info("[%s] Device ID changed: %s -> %s, clearing crypto store", self.name, old_device_id, dev_id)
                        self._client = None
                        if old_device_id:
                            import glob as _glob
                            for db_file in _glob.glob(os.path.join(store_path, "*.db")):
                                logger.info("[%s] Removing old store: %s", self.name, db_file)
                                os.remove(db_file)
                        self._client = AsyncClient(
                            homeserver=self.config["homeserver"],
                            user=self.config["user_id"],
                            device_id=dev_id,
                            store_path=store_path,
                            config=client_config,
                        )
                    self._client.restore_login(
                        user_id=self.config["user_id"],
                        device_id=dev_id,
                        access_token=token,
                    )
                    self.config["access_token"] = token
                    self.config["device_id"] = dev_id
                    self._token_obtained_at = time.monotonic()
                    await self._persist_device_id()
                except Exception as e:
                    raise RuntimeError(f"SSO login failed for {self.name}: {e}")
            else:
                password = self.config.get("password", "")
                if not password:
                    password = getpass.getpass(f"[{self.name}] Password for {self.config['user_id']}: ")

                if not self._client.access_token and not password:
                    raise RuntimeError(f"No password provided for {self.name}")

                if self._client.access_token:
                    logger.info("[%s] Logged in via SSO", self.name)
                else:
                    resp = await self._client.login(password)
                    if getattr(resp, "access_token", None):
                        self._client.access_token = resp.access_token
                        assigned_device_id = getattr(resp, "device_id", None)
                        if assigned_device_id and not self.config.get("device_id"):
                            self._client.device_id = assigned_device_id
                            self.config["device_id"] = assigned_device_id
                            logger.info("[%s] Server assigned device_id: %s", self.name, assigned_device_id)
                            await self._persist_device_id()
                        logger.info("[%s] Logged in successfully", self.name)
                    elif getattr(resp, "status_code", None) == "M_UNRECOGNIZED":
                        legacy_auth = {
                            "type": "m.login.password",
                            "user": self.config["user_id"],
                            "password": password,
                        }
                        device_id_from_config = self.config.get("device_id") or ""
                        if device_id_from_config:
                            legacy_auth["device_id"] = device_id_from_config
                        resp = await self._client.login_raw(legacy_auth)
                        if getattr(resp, "access_token", None):
                            self._client.access_token = resp.access_token
                            assigned_device_id = getattr(resp, "device_id", None)
                            if assigned_device_id and not self.config.get("device_id"):
                                self._client.device_id = assigned_device_id
                                self.config["device_id"] = assigned_device_id
                                logger.info("[%s] Server assigned device_id: %s", self.name, assigned_device_id)
                                await self._persist_device_id()
                            logger.info("[%s] Logged in successfully (legacy login format)", self.name)
                        else:
                            logger.error("[%s] Login failed: %s", self.name, resp)
                            raise RuntimeError(f"Login failed for {self.name}: {resp}")
                    else:
                        logger.error("[%s] Login failed: %s", self.name, resp)
                        raise RuntimeError(f"Login failed for {self.name}: {resp}")

        # Upload unconditionally: should_upload_keys may be stale when the
        # homeserver omits one_time_key_counts from the sync response.
        try:
            await self._client.keys_upload()
        except Exception as e:
            logger.warning("[%s] Key upload failed: %s", self.name, e)
        if self._client.should_query_keys:
            await self._client.keys_query()

        await self._check_identity_key_consistency()

        await self._import_keys_if_configured()
        saved_token = self._state.load_sync_token(self.name)
        await self._verify_connection()
        return saved_token

    async def _persist_device_id(self) -> None:
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
            access_token = self.config.get("access_token", "")
            if access_token:
                section["access_token"] = access_token
            refresh_token = self.config.get("refresh_token", "")
            if refresh_token:
                section["refresh_token"] = refresh_token
            file_config[self.name] = section
            tmp_path = self._config_path + ".tmp"
            with open(tmp_path, "w") as f:
                yaml.dump(file_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            os.replace(tmp_path, self._config_path)
            os.chmod(self._config_path, stat.S_IRUSR | stat.S_IWUSR)
            logger.info("[%s] device_id + access_token saved to %s", self.name, self._config_path)
        except Exception as e:
            logger.error("[%s] Failed to persist config: %s", self.name, e)

    def _local_identity_keys(self) -> dict:
        """The bridge's own olm identity keys, or {} if crypto isn't loaded."""
        client = self._get_client()
        olm = getattr(client, "olm", None)
        if olm is None or getattr(olm, "account", None) is None:
            return {}
        return dict(olm.account.identity_keys)

    async def _check_identity_key_consistency(self) -> None:
        client = self._get_client()
        local_curve = self._local_identity_keys().get("curve25519", "")
        if not local_curve or not client.device_id:
            return
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.config['homeserver']}/_matrix/client/v3/keys/query",
                    headers={"Authorization": f"Bearer {client.access_token}"},
                    json={"device_keys": {client.user_id: [client.device_id]}},
                ) as r:
                    if r.status != 200:
                        return
                    data = await r.json()
            server_curve = (
                data.get("device_keys", {})
                .get(client.user_id, {})
                .get(client.device_id, {})
                .get("keys", {})
                .get(f"curve25519:{client.device_id}", "")
            )
            if not server_curve:
                return
            if server_curve != local_curve:
                logger.error(
                    "[%s] IDENTITY KEY MISMATCH for device %s! local=%s server=%s — "
                    "the server advertises keys this bridge does not own (usually an "
                    "Element Web SSO login uploaded its own keys for this device_id). "
                    "Senders encrypt room keys to the server's key, so NOTHING can be "
                    "decrypted. Fix: sign out this session in Element (deleting the "
                    "device server-side), delete the local store/ directory, clear "
                    "device_id/access_token in config, and restart to create a fresh "
                    "device. Deleting only the store/ directory will NOT fix it.",
                    self.name, client.device_id,
                    local_curve[:16] + "...", server_curve[:16] + "...",
                )
            else:
                logger.info("[%s] Identity key verified OK (curve25519=%s...)", self.name, local_curve[:16])
        except Exception:
            pass

    async def _ensure_backup_key(self) -> bool:
        if self._backup_priv_bytes:
            return True
        recovery_key = self.config.get("recovery_key", "")
        if not recovery_key:
            return False
        try:
            from bridge.key_backup import derive_backup_key
            priv, version = await derive_backup_key(
                homeserver=self.config["homeserver"],
                access_token=self._get_client().access_token,
                user_id=self.config["user_id"],
                recovery_key=recovery_key,
            )
            self._backup_priv_bytes = priv
            self._backup_version = version
            logger.info("[%s] Backup key derived and cached (version=%s)", self.name, version)
            return True
        except Exception as e:
            logger.debug("[%s] Failed to derive backup key: %s", self.name, e)
            return False

    async def restore_session_from_backup(self, room_id: str, session_id: str) -> bool:
        if not await self._ensure_backup_key():
            return False
        try:
            from bridge.key_backup import fetch_session_from_backup, _create_key_export_data
            session = await fetch_session_from_backup(
                homeserver=self.config["homeserver"],
                access_token=self._get_client().access_token,
                backup_priv=self._backup_priv_bytes,
                room_id=room_id,
                session_id=session_id,
                version=self._backup_version,
            )
            if not session:
                return False
            client = self._get_client()
            tmp_pass = base64.b64encode(os.urandom(24)).decode()
            export_str = _create_key_export_data([session], tmp_pass)
            fd, tmp_path = tempfile.mkstemp(suffix=".txt")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(export_str)
                await client.import_keys(tmp_path, tmp_pass)
                logger.info("[%s] Imported session %s... from backup", self.name, session_id[:16])
                return True
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception as e:
            logger.debug("[%s] Failed to restore session %s from backup: %s", self.name, session_id[:16], e)
            return False

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
            if resp.device_id and resp.device_id != client.device_id:
                logger.info("[%s] Updating device_id from whoami: %s -> %s", self.name, client.device_id, resp.device_id)
                client.device_id = resp.device_id
                self.config["device_id"] = resp.device_id
                await self._persist_device_id()
            logger.log(ALWAYS, "[%s] Auth verified: user=%s device=%s", self.name, resp.user_id, resp.device_id)
        elif "M_UNKNOWN_TOKEN" in str(resp):
            logger.log(ALWAYS, "[%s] Token expired, attempting SSO re-login...", self.name)
            refreshed = await self._refresh_token_via_sso()
            if refreshed:
                resp2 = await client.whoami()
                if isinstance(resp2, WhoamiResponse):
                    if resp2.device_id and resp2.device_id != client.device_id:
                        logger.info("[%s] Updating device_id from whoami: %s -> %s", self.name, client.device_id, resp2.device_id)
                        client.device_id = resp2.device_id
                        self.config["device_id"] = resp2.device_id
                        await self._persist_device_id()
                    logger.log(ALWAYS, "[%s] Re-auth verified: user=%s device=%s", self.name, resp2.user_id, resp2.device_id)
                    return
            raise RuntimeError(f"Auth check failed for {self.name}: {resp}")
        else:
            logger.log(ALWAYS, "[%s] Auth check failed: %s — check access_token and homeserver", self.name, resp)
            raise RuntimeError(f"Auth check failed for {self.name}: {resp}")

    async def import_key_file(self) -> bool:
        """Import encryption keys from a file exported by Element."""
        key_file = self.config.get("key_import_file", "")
        passphrase = self.config.get("key_import_passphrase", "")
        if not key_file or not passphrase:
            return False
        if not os.path.isfile(key_file):
            logger.warning("[%s] key_import_file not found: %s", self.name, key_file)
            return False
        try:
            await self._get_client().import_keys(key_file, passphrase)
            logger.log(ALWAYS, "[%s] Encryption keys imported from %s", self.name, key_file)
            return True
        except Exception as e:
            logger.error("[%s] Failed to import key file: %s", self.name, e)
            return False

    async def restore_key_backup(self) -> int:
        """Fetch megolm sessions from server key backup using the configured recovery_key."""
        recovery_key = self.config.get("recovery_key", "")
        if not recovery_key:
            logger.info("[%s] No recovery_key configured, skipping key backup restore", self.name)
            return 0
        from bridge.key_backup import restore_key_backup
        client = self._get_client()
        try:
            n = await restore_key_backup(
                client=client,
                homeserver=self.config["homeserver"],
                access_token=client.access_token,
                recovery_key=recovery_key,
            )
            if n:
                logger.log(ALWAYS, "[%s] Key backup restored: %d sessions imported", self.name, n)
            return n
        except Exception as e:
            logger.error("[%s] Key backup restore failed: %s", self.name, e)
            return 0

    async def _refresh_token_via_sso(self) -> bool:
        # Try silent refresh_token first — avoids launching a browser.
        refresh_token = self.config.get("refresh_token", "")
        is_oidc = refresh_token.startswith("{") if refresh_token else False
        if refresh_token and (is_oidc or not self._matrix_refresh_unsupported):
            from backends.sso_login import _try_refresh_token, _save_cached_token
            result = await _try_refresh_token(self.config["homeserver"], refresh_token)
            if result and result[0] == "unsupported":
                self._matrix_refresh_unsupported = True
                logger.info("[%s] Matrix refresh disabled for this session", self.name)
            elif result:
                new_token, new_refresh, _ = result
                self._client.access_token = new_token
                self.config["access_token"] = new_token
                self.config["refresh_token"] = new_refresh
                self._token_obtained_at = time.monotonic()
                await self._persist_device_id()
                logger.info("[%s] Token silently refreshed via refresh_token", self.name)
                return True
            else:
                logger.info("[%s] refresh_token expired, falling back to SSO browser login", self.name)

        element_url = self.config.get("element_url", "")
        if not element_url:
            logger.error("[%s] No 'element_url' configured, cannot refresh token via SSO", self.name)
            return False
        try:
            from backends.sso_login import sso_login
            token, device_id, refresh_tok = await sso_login(
                homeserver=self.config["homeserver"],
                element_url=element_url,
                user_id=self.config["user_id"],
                device_id=self.config.get("device_id") or "",
                username=self.config.get("sso_username", ""),
                password=self.config.get("password", ""),
                recovery_key=self.config.get("recovery_key", ""),
            )
            self._client.access_token = token
            self._client.device_id = device_id
            self.config["access_token"] = token
            self.config["device_id"] = device_id
            if refresh_tok:
                self.config["refresh_token"] = refresh_tok
            self._token_obtained_at = time.monotonic()
            await self._persist_device_id()
            logger.info("[%s] Token refreshed via SSO browser", self.name)
            return True
        except Exception as e:
            logger.error("[%s] SSO token refresh failed: %s", self.name, e)
            return False

    # ---------------------------------------------------------------- lifecycle

    async def _periodic_token_refresh(self) -> None:
        """Proactively refresh the token before it expires. Only runs if SSO is configured."""
        if not self.config.get("element_url"):
            return
        REFRESH_INTERVAL = 240  # target: refresh at 4 minutes for a 5-minute token
        await asyncio.sleep(30)   # short initial delay, then poll frequently
        while self._running:
            age = time.monotonic() - self._token_obtained_at
            if self._token_obtained_at > 0 and age >= REFRESH_INTERVAL:
                logger.info("[%s] Proactive token refresh (age=%.0fs)", self.name, age)
                try:
                    await self._refresh_token_via_sso()
                except Exception as e:
                    logger.error("[%s] Proactive token refresh failed: %s", self.name, e)
            await asyncio.sleep(30)

    async def stop(self) -> None:
        self._running = False
        for attr in ("_flush_task", "_key_upload_task", "_sync_task", "_call_cleanup_task", "_token_refresh_task", "_key_backup_restore_task"):
            task = getattr(self, attr, None)
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
                devices = list(client.device_store.active_user_devices(sender))
                if devices:
                    await client.keys_claim({sender: [d.id for d in devices]})
            except Exception:
                pass
            try:
                await client.request_room_key(enc_event)
                entry["last_requested"] = now
                age = int(now - entry["first_seen"])
                logger.info(
                    "[%s] Re-requested room key for session %s (pending %ds, sender %s)",
                    self.name, session_id, age, sender,
                )
            except Exception as e:
                msg = str(e)
                if "already sent" in msg.lower():
                    logger.debug("[%s] Key request already pending for session %s", self.name, session_id)
                else:
                    logger.warning("[%s] Re-request failed for session %s: %s", self.name, session_id, e)
            try:
                await client.send_to_device_messages()
            except Exception:
                pass
        await self._request_keys_from_own_devices()

    def _trust_own_devices(self) -> None:
        """Locally mark our own account's other devices as verified.

        nio drops m.forwarded_room_key from any device not flagged verified in
        its store (_should_accept_forward), so without this the key responses
        our own Element sessions send back are silently discarded with
        "Received a forwarded room key from a untrusted device".
        """
        client = self._get_client()
        own_user_id = self.config.get("user_id", "")
        trusted = 0
        for device in client.device_store.active_user_devices(own_user_id):
            if device.id == client.device_id or device.verified:
                continue
            try:
                if client.verify_device(device):
                    trusted += 1
            except Exception:
                pass
        if trusted:
            logger.info(
                "[%s] Locally trusted %d own device(s) so their key forwards are accepted",
                self.name, trusted,
            )

    def _register_key_request(self, session_id: str, enc_event, request_id: str) -> None:
        """Record an outgoing key request in nio's store.

        nio only accepts a forwarded room key if its session_id appears in
        olm.outgoing_key_requests; requests sent as raw to-device messages
        bypass that registry, so the answer would be ignored with
        "Ignoring session key we have not requested".
        """
        client = self._get_client()
        olm = getattr(client, "olm", None)
        if olm is None or session_id in olm.outgoing_key_requests:
            return
        try:
            from nio.crypto import OutgoingKeyRequest
            req = OutgoingKeyRequest(
                request_id=request_id,
                session_id=session_id,
                room_id=enc_event.room_id,
                algorithm=getattr(enc_event, "algorithm", "m.megolm.v1.aes-sha2"),
            )
            olm.outgoing_key_requests[session_id] = req
            client.store.add_outgoing_key_request(req)
        except Exception as e:
            logger.debug("[%s] Could not register key request for %s: %s", self.name, session_id, e)

    async def _request_keys_from_own_devices(self) -> None:
        client = self._get_client()
        own_user_id = self.config.get("user_id", "")
        own_device_id = client.device_id or ""

        self._trust_own_devices()

        own_other_devices = [
            d.id for d in client.device_store.active_user_devices(own_user_id)
            if d.id != own_device_id
        ]
        if not own_other_devices or not self._pending_encrypted:
            return

        sent = 0
        for session_id, entry in self._pending_encrypted.items():
            events = entry.get("events", [])
            if not events:
                continue
            _, enc_event = events[0]

            self._register_key_request(session_id, enc_event, f"mxbridge_{session_id[:16]}")

            request_content = {
                "action": "request",
                "body": {
                    "algorithm": getattr(enc_event, "algorithm", ""),
                    "room_id": enc_event.room_id,
                    "sender_key": getattr(enc_event, "sender_key", ""),
                    "session_id": session_id,
                },
                "request_id": f"mxbridge_{session_id[:16]}",
                "requesting_device_id": own_device_id,
            }

            for device_id in own_other_devices:
                try:
                    await client.to_device(ToDeviceMessage(
                        type="m.room_key_request",
                        recipient=own_user_id,
                        recipient_device=device_id,
                        content=request_content,
                    ))
                    sent += 1
                except Exception:
                    pass

        if sent:
            logger.info(
                "[%s] Sent key requests to %d own device(s) for %d pending session(s)",
                self.name, len(own_other_devices), len(self._pending_encrypted),
            )

    async def _enqueue_pending_encrypted(self, room, event, error) -> None:
        session_id = getattr(event, "session_id", None)
        if not session_id:
            return
        now = time.monotonic()
        if session_id not in self._pending_encrypted and len(self._pending_encrypted) >= MAX_PENDING_SESSIONS:
            oldest = next(iter(self._pending_encrypted))
            old_entry = self._pending_encrypted.pop(oldest)
            for _, ev in old_entry.get("events", []):
                self._pending_event_ids.discard(ev.event_id)
            logger.warning("[%s] pending_encrypted full, evicted oldest session %s", self.name, oldest)
        entry = self._pending_encrypted.setdefault(session_id, {
            "events": [], "first_seen": now, "last_requested": 0.0,
        })
        if event.event_id not in self._pending_event_ids:
            self._pending_event_ids.add(event.event_id)
            entry["events"].append((room, event))
            if len(entry["events"]) > MAX_PENDING_EVENTS_PER_SESSION:
                discarded = entry["events"].pop(0)
                self._pending_event_ids.discard(discarded[1].event_id)
                logger.warning(
                    "[%s] session %s exceeds %d pending events, discarded oldest %s",
                    self.name, session_id, MAX_PENDING_EVENTS_PER_SESSION,
                    discarded[1].event_id,
                )

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
                    logger.info("[%s] Claimed one-time keys for %s (%d device(s))", self.name, event.sender, len(devices))
            except Exception:
                pass
        if now - entry["last_requested"] >= KEY_RECHECK_INTERVAL:
            try:
                await client.request_room_key(event)
                entry["last_requested"] = now
                logger.info("[%s] Requested missing room key for session %s (sender %s)", self.name, session_id, event.sender)
            except Exception as req_err:
                if "already sent" not in str(req_err).lower():
                    logger.warning("[%s] Failed to request room key: %s", self.name, req_err)
            try:
                await client.send_to_device_messages()
            except Exception:
                pass

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
        backoff = 0
        _last_key_upload = 0.0
        while self._running:
            try:
                resp = await client.sync(timeout=SYNC_TIMEOUT)
                if isinstance(resp, SyncResponse):
                    backoff = 0
                    if resp.next_batch:
                        await self._state.save_sync_token(self.name, resp.next_batch)
                    self._invalidate_own_displayname_cache()
                    await self._after_sync(client, resp)
                    try:
                        if client.outgoing_to_device_messages:
                            await client.send_to_device_messages()
                        now = time.monotonic()
                        if client.should_upload_keys and now - _last_key_upload >= KEY_RECHECK_INTERVAL:
                            await client.keys_upload()
                            _last_key_upload = now
                        if client.should_query_keys:
                            await client.keys_query()
                        if client.should_claim_keys:
                            users_to_claim = client.get_users_for_key_claiming()
                            logger.info("[%s] Claiming keys for: %s", self.name, users_to_claim)
                            await client.keys_claim(users_to_claim)
                    except Exception as e:
                        logger.warning("[%s] Key maintenance error: %s", self.name, e)
                else:
                    backoff = min(backoff + 5, 60)
                    tr = getattr(resp, "transport_response", None)
                    if tr is not None:
                        status = getattr(tr, "status", None)
                        body = ""
                        try:
                            body = await tr.text()
                        except Exception:
                            body = "<unreadable>"
                        if "M_UNKNOWN_TOKEN" in str(resp):
                            logger.warning("[%s] Token expired during sync, attempting SSO re-login...", self.name)
                            refreshed = await self._refresh_token_via_sso()
                            if refreshed:
                                backoff = 0
                                continue
                        logger.warning(
                            "[%s] Sync error (HTTP %s): %s | body: %.500s",
                            self.name, status, resp, body,
                        )
                    else:
                        if "M_UNKNOWN_TOKEN" in str(resp):
                            logger.warning("[%s] Token expired during sync, attempting SSO re-login...", self.name)
                            refreshed = await self._refresh_token_via_sso()
                            if refreshed:
                                backoff = 0
                                continue
                        logger.warning("[%s] Sync error: %s", self.name, resp)
                    await asyncio.sleep(backoff)
            except Exception as e:
                backoff = min(backoff + 10, 120)
                logger.error("[%s] Sync exception: %s", self.name, e)
                await asyncio.sleep(backoff)

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
