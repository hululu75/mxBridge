"""P1-3: get_stats uses single SQL query."""
from __future__ import annotations

from datetime import datetime, timezone

from bridge.message_store import Message


class TestGetStats:
    def _create_msg(self, event_id, direction="forward", timestamp=None):
        Message.create(
            timestamp=timestamp or datetime.now(timezone.utc),
            direction=direction,
            source_room_id="!room1:srv",
            source_room_name="Room",
            sender="@alice:srv",
            sender_displayname="Alice",
            text="hello",
            msgtype="m.text",
            event_id=event_id,
        )

    def test_empty_database(self, store):
        stats = store.get_stats()
        assert stats["total_messages"] == 0
        assert stats["total_rooms"] == 0
        assert stats["forward_count"] == 0
        assert stats["reply_count"] == 0
        assert stats["earliest_message"] is None
        assert stats["latest_message"] is None

    def test_single_message(self, store):
        ts = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        self._create_msg("$e1", timestamp=ts)
        stats = store.get_stats()
        assert stats["total_messages"] == 1
        assert stats["total_rooms"] == 1
        assert stats["forward_count"] == 1
        assert stats["reply_count"] == 0
        assert stats["earliest_message"] is not None
        assert stats["latest_message"] is not None

    def test_mixed_directions(self, store):
        t1 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2025, 6, 1, tzinfo=timezone.utc)
        t3 = datetime(2025, 12, 1, tzinfo=timezone.utc)
        self._create_msg("$e1", direction="forward", timestamp=t1)
        self._create_msg("$e2", direction="reply", timestamp=t2)
        self._create_msg("$e3", direction="forward", timestamp=t3)
        stats = store.get_stats()
        assert stats["total_messages"] == 3
        assert stats["forward_count"] == 2
        assert stats["reply_count"] == 1

    def test_multiple_rooms(self, store):
        for i, rid in enumerate(["!r1:srv", "!r2:srv", "!r3:srv"]):
            Message.create(
                timestamp=datetime.now(timezone.utc),
                direction="forward",
                source_room_id=rid,
                source_room_name=rid,
                sender="@a:srv",
                sender_displayname="A",
                text="hi",
                msgtype="m.text",
                event_id=f"$e{i}",
            )
        stats = store.get_stats()
        assert stats["total_messages"] == 3
        assert stats["total_rooms"] == 3

    def test_earliest_and_latest(self, store):
        t1 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2025, 12, 31, tzinfo=timezone.utc)
        self._create_msg("$early", timestamp=t1)
        self._create_msg("$late", timestamp=t2)
        stats = store.get_stats()
        assert stats["earliest_message"] is not None
        assert stats["latest_message"] is not None
        assert stats["earliest_message"] != stats["latest_message"]
