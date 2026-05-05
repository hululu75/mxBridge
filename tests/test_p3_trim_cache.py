from __future__ import annotations

import collections

from backends.base import BaseBackend
from backends.matrix_base import MAX_DISPLAYNAME_CACHE, MAX_ROOM_NAME_CACHE


def _make_backend():
    class Concrete(BaseBackend):
        async def start(self): pass
        async def stop(self): pass
        async def send_message(self, room_id, text, msgtype="m.text"): return ""
        async def send_media(self, room_id, data, mimetype, filename, msgtype="m.file", extra_info=None): return ""
        async def redact_event(self, room_id, event_id, reason=None): return ""
        async def edit_message(self, room_id, event_id, new_text, msgtype="m.notice"): return ""
        async def resolve_room_id(self, room_alias_or_id): return None

    from backends.matrix_base import MatrixBackend

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
    b._running = False
    b._state = None
    b._flush_task = None
    b._sync_task = None
    b._key_upload_task = None
    b._call_cleanup_task = None
    b._pending_encrypted = {}
    b._pending_event_ids = set()
    b._config_path = None
    return b


class TestTrimCacheDisplayname:
    def test_evicts_oldest_when_over_limit(self):
        b = _make_backend()
        limit = 5
        for i in range(limit + 3):
            b._displayname_cache[f"@u{i}:srv"] = f"Name{i}"
            b._displayname_order.append(f"@u{i}:srv")
            b._trim_cache(b._displayname_cache, b._displayname_order, limit)
        assert len(b._displayname_cache) <= limit
        assert f"@u0:srv" not in b._displayname_cache
        assert f"@u{limit + 2}:srv" in b._displayname_cache

    def test_no_eviction_when_under_limit(self):
        b = _make_backend()
        b._displayname_cache["@a:srv"] = "A"
        b._displayname_order.append("@a:srv")
        b._trim_cache(b._displayname_cache, b._displayname_order, 5)
        assert "@a:srv" in b._displayname_cache

    def test_order_and_cache_stay_consistent(self):
        b = _make_backend()
        limit = 3
        for i in range(10):
            key = f"@u{i}:srv"
            b._displayname_cache[key] = f"Name{i}"
            if key not in b._displayname_order:
                b._displayname_order.append(key)
            b._trim_cache(b._displayname_cache, b._displayname_order, limit)
        assert len(b._displayname_cache) == len(b._displayname_order)
        for key in b._displayname_order:
            assert key in b._displayname_cache


class TestTrimCacheRoomName:
    def test_evicts_oldest_room_names(self):
        b = _make_backend()
        limit = 5
        for i in range(limit + 5):
            b._room_name_cache[f"!room{i}:srv"] = f"Room{i}"
            b._room_name_order.append(f"!room{i}:srv")
            b._trim_cache(b._room_name_cache, b._room_name_order, limit)
        assert len(b._room_name_cache) <= limit


class TestInvalidateOwnDisplayname:
    def test_invalidate_removes_from_both_cache_and_order(self):
        b = _make_backend()
        b._displayname_cache["__own__"] = "Bot"
        b._displayname_order.append("__own__")
        b._invalidate_own_displayname_cache()
        assert "__own__" not in b._displayname_cache
        assert "__own__" not in b._displayname_order

    def test_invalidate_noop_when_not_present(self):
        b = _make_backend()
        b._invalidate_own_displayname_cache()
        assert "__own__" not in b._displayname_cache
        assert len(b._displayname_order) == 0
