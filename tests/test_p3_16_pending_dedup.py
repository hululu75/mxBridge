"""P3-16: _enqueue_pending_encrypted uses O(1) set for dedup."""
from __future__ import annotations

import collections
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backends.base import BaseBackend
from backends.matrix_base import MAX_PENDING_SESSIONS, MatrixBackend


def _make_testable_backend():
    class Concrete(BaseBackend):
        async def start(self): pass
        async def stop(self): pass
        async def send_message(self, room_id, text, msgtype="m.text"): return ""
        async def send_media(self, room_id, data, mimetype, filename, msgtype="m.file", extra_info=None): return ""
        async def redact_event(self, room_id, event_id, reason=None): return ""
        async def edit_message(self, room_id, event_id, new_text, msgtype="m.notice"): return ""
        async def resolve_room_id(self, room_alias_or_id): return None

    class Testable(MatrixBackend, Concrete):
        pass

    b = Testable.__new__(Testable)
    b.name = "test"
    b.config = {"user_id": "@bot:srv", "device_id": "DEV"}
    b._message_callback = None
    b._client = MagicMock()
    b._client.users_for_key_query = MagicMock()
    b._client.keys_query = AsyncMock()
    b._client.device_store = MagicMock()
    b._client.device_store.active_user_devices = MagicMock(return_value=[])
    b._client.keys_claim = AsyncMock()
    b._client.request_room_key = AsyncMock()
    b._displayname_cache = {}
    b._room_name_cache = {}
    b._displayname_order = collections.deque()
    b._room_name_order = collections.deque()
    b._running = True
    b._state = MagicMock()
    b._flush_task = None
    b._sync_task = None
    b._key_upload_task = None
    b._call_cleanup_task = None
    b._pending_encrypted = {}
    b._pending_event_ids = set()
    b._config_path = None
    return b


def _make_event(event_id, session_id="sess1"):
    event = MagicMock()
    event.event_id = event_id
    event.session_id = session_id
    event.sender = "@alice:srv"
    return event


class TestPendingEncryptedDedup:
    @pytest.mark.asyncio
    async def test_duplicate_event_not_added(self):
        b = _make_testable_backend()
        event = _make_event("$e1")
        room = MagicMock()

        await b._enqueue_pending_encrypted(room, event, "error")
        entry = b._pending_encrypted["sess1"]
        assert len(entry["events"]) == 1

        await b._enqueue_pending_encrypted(room, event, "error")
        assert len(entry["events"]) == 1
        assert b._pending_event_ids == {"$e1"}

    @pytest.mark.asyncio
    async def test_different_events_same_session(self):
        b = _make_testable_backend()
        e1 = _make_event("$e1")
        e2 = _make_event("$e2")
        room = MagicMock()

        await b._enqueue_pending_encrypted(room, e1, "error")
        await b._enqueue_pending_encrypted(room, e2, "error")
        assert len(b._pending_encrypted["sess1"]["events"]) == 2
        assert b._pending_event_ids == {"$e1", "$e2"}

    @pytest.mark.asyncio
    async def test_eviction_clears_event_ids(self):
        b = _make_testable_backend()
        room = MagicMock()

        for i in range(MAX_PENDING_SESSIONS):
            event = _make_event(f"$e{i}", session_id=f"sess_{i:04d}")
            await b._enqueue_pending_encrypted(room, event, "error")

        assert len(b._pending_encrypted) == MAX_PENDING_SESSIONS
        assert len(b._pending_event_ids) == MAX_PENDING_SESSIONS

        new_event = _make_event("$extra", session_id="sess_new")
        await b._enqueue_pending_encrypted(room, new_event, "error")

        assert len(b._pending_encrypted) == MAX_PENDING_SESSIONS
        assert "$e0" not in b._pending_event_ids
        assert "$extra" in b._pending_event_ids

    @pytest.mark.asyncio
    async def test_no_session_id_ignored(self):
        b = _make_testable_backend()
        event = MagicMock()
        event.event_id = "$e1"
        event.session_id = None
        event.sender = "@alice:srv"
        room = MagicMock()

        await b._enqueue_pending_encrypted(room, event, "error")
        assert len(b._pending_encrypted) == 0
        assert len(b._pending_event_ids) == 0

    @pytest.mark.asyncio
    async def test_pop_clears_event_ids(self):
        b = _make_testable_backend()
        e1 = _make_event("$e1")
        room = MagicMock()
        await b._enqueue_pending_encrypted(room, e1, "error")

        entry = b._pending_encrypted.pop("sess1")
        for _, ev in entry["events"]:
            b._pending_event_ids.discard(ev.event_id)

        assert "$e1" not in b._pending_event_ids

    @pytest.mark.asyncio
    async def test_different_sessions_independent(self):
        b = _make_testable_backend()
        room = MagicMock()
        e1 = _make_event("$e1", session_id="sess_a")
        e2 = _make_event("$e2", session_id="sess_b")

        await b._enqueue_pending_encrypted(room, e1, "error")
        await b._enqueue_pending_encrypted(room, e2, "error")

        assert len(b._pending_encrypted) == 2
        assert b._pending_event_ids == {"$e1", "$e2"}
