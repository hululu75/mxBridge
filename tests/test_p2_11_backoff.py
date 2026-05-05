"""P2-11: _sync_loop uses exponential backoff."""
from __future__ import annotations

import asyncio
import collections
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nio import SyncResponse


def _make_concrete_matrix_backend():
    from backends.base import BaseBackend
    from backends.matrix_base import MatrixBackend

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
    b.config = {"user_id": "@bot:srv"}
    b._message_callback = None
    b._client = None
    b._displayname_cache = {}
    b._room_name_cache = {}
    b._displayname_order = collections.deque()
    b._room_name_order = collections.deque()
    b._running = True
    b._state = AsyncMock()
    b._flush_task = None
    b._sync_task = None
    b._key_upload_task = None
    b._call_cleanup_task = None
    b._pending_encrypted = {}
    b._pending_event_ids = set()
    b._config_path = None
    return b


class TestSyncLoopBackoff:
    @pytest.mark.asyncio
    async def test_backoff_resets_on_success(self):
        backend = _make_concrete_matrix_backend()
        client = MagicMock()
        call_count = 0

        async def fake_sync(timeout):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                backend._running = False
            resp = MagicMock(spec=SyncResponse)
            resp.next_batch = f"token{call_count}"
            resp.rooms = MagicMock()
            resp.rooms.join = {}
            return resp

        client.sync = fake_sync
        client.outgoing_to_device_messages = []
        client.should_upload_keys = False
        client.should_query_keys = False
        client.should_claim_keys = False

        with patch.object(backend, '_get_client', return_value=client):
            await backend._sync_loop()

        backend._state.save_sync_token.assert_awaited()

    @pytest.mark.asyncio
    async def test_backoff_increases_on_exception(self):
        backend = _make_concrete_matrix_backend()
        client = MagicMock()

        sleep_times = []
        original_sleep = asyncio.sleep

        async def tracking_sleep(seconds):
            sleep_times.append(seconds)
            if len(sleep_times) >= 3:
                backend._running = False
            await original_sleep(0)

        client.sync = AsyncMock(side_effect=ConnectionError("network down"))

        with patch.object(backend, '_get_client', return_value=client), \
             patch('backends.matrix_base.asyncio.sleep', tracking_sleep):
            await backend._sync_loop()

        assert len(sleep_times) >= 3
        assert sleep_times[1] > sleep_times[0]
        assert sleep_times[2] > sleep_times[1]

    @pytest.mark.asyncio
    async def test_backoff_caps_at_max(self):
        backend = _make_concrete_matrix_backend()
        client = MagicMock()

        sleep_times = []
        original_sleep = asyncio.sleep

        async def tracking_sleep(seconds):
            sleep_times.append(seconds)
            if len(sleep_times) >= 5:
                backend._running = False
            await original_sleep(0)

        client.sync = AsyncMock(side_effect=ConnectionError("down"))

        with patch.object(backend, '_get_client', return_value=client), \
             patch('backends.matrix_base.asyncio.sleep', tracking_sleep):
            await backend._sync_loop()

        assert max(sleep_times) <= 120
