"""P2-6: _search_like and get_room_history use raw SQL instead of double ORM scan."""
from __future__ import annotations

from datetime import datetime, timezone

from bridge.message_store import Message


def _insert_msgs(store, count, room_id="!room1:srv", text_prefix="msg", id_offset=0):
    for i in range(count):
        Message.create(
            timestamp=datetime(2025, 1, 1, 0, i % 60, 0, tzinfo=timezone.utc),
            direction="forward",
            source_room_id=room_id,
            source_room_name="Room",
            sender="@alice:srv",
            sender_displayname="Alice",
            text=f"{text_prefix}_{i}",
            msgtype="m.text",
            event_id=f"$e_{room_id}_{i + id_offset}",
        )


class TestSearchLike:
    def test_empty_query_returns_all(self, store):
        _insert_msgs(store, 5)
        result = store._search_like("", "", "", None, None, 1, 10)
        assert result["total"] == 5
        assert len(result["results"]) == 5

    def test_text_search(self, store):
        _insert_msgs(store, 5)
        result = store._search_like("msg_3", "", "", None, None, 1, 10)
        assert result["total"] == 1
        assert result["results"][0]["text"] == "msg_3"

    def test_pagination(self, store):
        _insert_msgs(store, 10)
        page1 = store._search_like("", "", "", None, None, 1, 3)
        page2 = store._search_like("", "", "", None, None, 2, 3)
        assert page1["total"] == 10
        assert len(page1["results"]) == 3
        assert page2["total"] == 10
        assert len(page2["results"]) == 3
        assert page1["results"][0]["id"] != page2["results"][0]["id"]

    def test_room_filter(self, store):
        _insert_msgs(store, 3, room_id="!room1:srv")
        _insert_msgs(store, 2, room_id="!room2:srv")
        result = store._search_like("", "!room1:srv", "", None, None, 1, 10)
        assert result["total"] == 3

    def test_sender_filter(self, store):
        _insert_msgs(store, 3)
        Message.create(
            timestamp=datetime.now(timezone.utc), direction="forward",
            source_room_id="!room1:srv", source_room_name="Room",
            sender="@bob:srv", sender_displayname="Bob",
            text="hi", msgtype="m.text", event_id="$bob1",
        )
        result = store._search_like("", "", "@bob:srv", None, None, 1, 10)
        assert result["total"] == 1

    def test_no_results(self, store):
        _insert_msgs(store, 3)
        result = store._search_like("nonexistent", "", "", None, None, 1, 10)
        assert result["total"] == 0
        assert result["results"] == []


class TestGetRoomHistory:
    def test_returns_paginated(self, store):
        _insert_msgs(store, 10, room_id="!room1:srv")
        result = store.get_room_history("!room1:srv", page=1, limit=5)
        assert result["total"] == 10
        assert len(result["results"]) == 5

    def test_ordered_ascending(self, store):
        _insert_msgs(store, 3, room_id="!room1:srv")
        result = store.get_room_history("!room1:srv", page=1, limit=10)
        timestamps = [r["timestamp"] for r in result["results"]]
        assert timestamps == sorted(timestamps)

    def test_empty_room(self, store):
        result = store.get_room_history("!empty:srv", page=1, limit=10)
        assert result["total"] == 0
        assert result["results"] == []

    def test_filters_by_room(self, store):
        _insert_msgs(store, 5, room_id="!room1:srv")
        _insert_msgs(store, 3, room_id="!room2:srv")
        result = store.get_room_history("!room1:srv", page=1, limit=10)
        assert result["total"] == 5
        for r in result["results"]:
            assert r["source_room_id"] == "!room1:srv"

    def test_page2_offset(self, store):
        _insert_msgs(store, 10, room_id="!room1:srv")
        p1 = store.get_room_history("!room1:srv", page=1, limit=5)
        p2 = store.get_room_history("!room1:srv", page=2, limit=5)
        ids_p1 = {r["id"] for r in p1["results"]}
        ids_p2 = {r["id"] for r in p2["results"]}
        assert ids_p1.isdisjoint(ids_p2)
