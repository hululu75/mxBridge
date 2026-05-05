"""P2-10: get_existing_event_ids preloads event IDs for O(1) dedup."""
from __future__ import annotations

from datetime import datetime, timezone

from bridge.message_store import Message


class TestGetExistingEventIds:
    def test_empty_room(self, store):
        result = store.get_existing_event_ids("!empty:srv")
        assert result == set()

    def test_returns_ids_for_room(self, store):
        for i in range(5):
            Message.create(
                timestamp=datetime.now(timezone.utc),
                direction="forward",
                source_room_id="!room1:srv",
                source_room_name="Room",
                sender="@a:srv",
                sender_displayname="A",
                text=f"msg{i}",
                msgtype="m.text",
                event_id=f"$e{i}",
            )
        result = store.get_existing_event_ids("!room1:srv")
        assert result == {f"$e{i}" for i in range(5)}

    def test_filters_by_room(self, store):
        for rid in ["!room1:srv", "!room2:srv"]:
            Message.create(
                timestamp=datetime.now(timezone.utc),
                direction="forward",
                source_room_id=rid,
                source_room_name="Room",
                sender="@a:srv",
                sender_displayname="A",
                text="hi",
                msgtype="m.text",
                event_id=f"$e_{rid}",
            )
        result = store.get_existing_event_ids("!room1:srv")
        assert result == {"$e_!room1:srv"}
        assert "$e_!room2:srv" not in result

    def test_performance_large_set(self, store):
        for i in range(1000):
            Message.create(
                timestamp=datetime.now(timezone.utc),
                direction="forward",
                source_room_id="!room1:srv",
                source_room_name="Room",
                sender="@a:srv",
                sender_displayname="A",
                text=f"msg{i}",
                msgtype="m.text",
                event_id=f"$e{i}",
            )
        result = store.get_existing_event_ids("!room1:srv")
        assert len(result) == 1000
        assert "$e999" in result
        assert "$e1000" not in result

    def test_exception_returns_empty_set(self, store):
        import bridge.message_store as ms
        orig = ms.Message.select
        def broken_select(*a, **kw):
            raise Exception("db error")
        ms.Message.select = broken_select
        try:
            result = store.get_existing_event_ids("!room1:srv")
            assert result == set()
        finally:
            ms.Message.select = orig
