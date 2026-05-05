from __future__ import annotations

import collections
from unittest.mock import AsyncMock, MagicMock

import pytest

from backends.base import BaseBackend
from backends.matrix_base import MAX_PENDING_EVENTS_PER_SESSION, MatrixBackend


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


class TestPendingEventsPerSessionTrim:
    @pytest.mark.asyncio
    async def test_excess_events_trimmed_from_session(self):
        b = _make_testable_backend()
        room = MagicMock()
        limit = MAX_PENDING_EVENTS_PER_SESSION

        for i in range(limit + 5):
            event = _make_event(f"$e{i}", session_id="sess1")
            await b._enqueue_pending_encrypted(room, event, "error")

        entry = b._pending_encrypted["sess1"]
        assert len(entry["events"]) <= limit
        assert f"$e0" not in b._pending_event_ids
        assert f"$e{limit + 4}" in b._pending_event_ids

    @pytest.mark.asyncio
    async def test_trim_preserves_latest_events(self):
        b = _make_testable_backend()
        room = MagicMock()
        limit = MAX_PENDING_EVENTS_PER_SESSION

        for i in range(limit + 3):
            event = _make_event(f"$e{i}", session_id="sess1")
            await b._enqueue_pending_encrypted(room, event, "error")

        entry = b._pending_encrypted["sess1"]
        event_ids = [ev.event_id for _, ev in entry["events"]]
        assert event_ids[-1] == f"$e{limit + 2}"

    @pytest.mark.asyncio
    async def test_no_trim_when_at_limit(self):
        b = _make_testable_backend()
        room = MagicMock()
        limit = MAX_PENDING_EVENTS_PER_SESSION

        for i in range(limit):
            event = _make_event(f"$e{i}", session_id="sess1")
            await b._enqueue_pending_encrypted(room, event, "error")

        entry = b._pending_encrypted["sess1"]
        assert len(entry["events"]) == limit


class TestStopGetattrFix:
    @pytest.mark.asyncio
    async def test_stop_without_init_does_not_raise(self):
        b = _make_testable_backend()
        b._client = None
        b._flush_task = None
        b._sync_task = None
        b._key_upload_task = None
        b._call_cleanup_task = None
        b._state = MagicMock()
        b._state.save_sync_token = AsyncMock()
        b._state.flush = AsyncMock()
        await b.stop()
        assert b._running is False

    @pytest.mark.asyncio
    async def test_stop_cancels_running_tasks(self):
        b = _make_testable_backend()
        b._client = MagicMock()
        b._client.next_batch = "token"
        b._client.close = AsyncMock()
        b._state = MagicMock()
        b._state.save_sync_token = AsyncMock()
        b._state.flush = AsyncMock()

        loop = __import__("asyncio").get_running_loop()
        b._flush_task = loop.create_task(__import__("asyncio").sleep(9999))
        b._sync_task = loop.create_task(__import__("asyncio").sleep(9999))
        b._key_upload_task = None
        b._call_cleanup_task = None

        await b.stop()
        assert b._running is False
        assert b._client is None
