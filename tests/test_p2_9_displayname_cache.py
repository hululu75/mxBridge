"""P2-9: get_own_displayname caches result, invalidates on sync."""
from __future__ import annotations

from unittest.mock import MagicMock

import collections

from backends.base import BaseBackend


def _make_concrete_backend():
    class ConcreteBackend(BaseBackend):
        async def start(self): pass
        async def stop(self): pass
        async def send_message(self, room_id, text, msgtype="m.text"): return ""
        async def send_media(self, room_id, data, mimetype, filename, msgtype="m.file", extra_info=None): return ""
        async def redact_event(self, room_id, event_id, reason=None): return ""
        async def edit_message(self, room_id, event_id, new_text, msgtype="m.notice"): return ""
        async def resolve_room_id(self, room_alias_or_id): return None

    from backends.matrix_base import MatrixBackend

    class TestableMatrixBackend(MatrixBackend, ConcreteBackend):
        pass

    b = TestableMatrixBackend.__new__(TestableMatrixBackend)
    b.name = "test"
    b.config = {"user_id": "@bot:srv"}
    b._message_callback = None
    b._client = None
    b._displayname_cache = {}
    b._room_name_cache = {}
    b._displayname_order = collections.deque()
    b._room_name_order = collections.deque()
    b._running = False
    b._state = MagicMock()
    b._flush_task = None
    b._sync_task = None
    b._key_upload_task = None
    b._call_cleanup_task = None
    b._pending_encrypted = {}
    b._pending_event_ids = set()
    b._config_path = None
    return b


class TestGetOwnDisplaynameCache:
    def test_no_client_returns_user_id(self):
        b = _make_concrete_backend()
        assert b.get_own_displayname() == "@bot:srv"

    def test_cached_value_returned_without_scanning_rooms(self):
        b = _make_concrete_backend()
        b._client = MagicMock()
        b._client.rooms = {}
        b._displayname_cache["__own__"] = "Bot Display"
        assert b.get_own_displayname() == "Bot Display"

    def test_scans_rooms_and_caches(self):
        b = _make_concrete_backend()
        client = MagicMock()
        client.user_id = "@bot:srv"
        user_mock = MagicMock()
        user_mock.display_name = "Bot Name"
        room_mock = MagicMock()
        room_mock.users = {"@bot:srv": user_mock}
        client.rooms = {"!room:srv": room_mock}
        b._client = client

        result = b.get_own_displayname()
        assert result == "Bot Name"
        assert b._displayname_cache["__own__"] == "Bot Name"

    def test_falls_back_to_user_id_when_no_displayname(self):
        b = _make_concrete_backend()
        client = MagicMock()
        client.user_id = "@bot:srv"
        user_mock = MagicMock()
        user_mock.display_name = None
        room_mock = MagicMock()
        room_mock.users = {"@bot:srv": user_mock}
        client.rooms = {"!room:srv": room_mock}
        b._client = client
        assert b.get_own_displayname() == "@bot:srv"
        assert "__own__" not in b._displayname_cache

    def test_invalidate_clears_cache(self):
        b = _make_concrete_backend()
        b._displayname_cache["__own__"] = "Bot"
        b._invalidate_own_displayname_cache()
        assert "__own__" not in b._displayname_cache

    def test_invalidate_noop_when_empty(self):
        b = _make_concrete_backend()
        b._invalidate_own_displayname_cache()
