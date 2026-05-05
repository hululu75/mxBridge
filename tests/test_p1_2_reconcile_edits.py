"""P1-2: reconcile_edits uses batch operations instead of N+1."""
from __future__ import annotations

from datetime import datetime, timezone

from bridge.message_store import Message, db


class TestReconcileEdits:
    def _create_msg(self, event_id, direction="forward", text="hello",
                    edit_of="", timestamp=None):
        Message.create(
            timestamp=timestamp or datetime.now(timezone.utc),
            direction=direction,
            source_room_id="!room:srv",
            source_room_name="Room",
            sender="@alice:srv",
            sender_displayname="Alice",
            text=text,
            msgtype="m.text",
            event_id=event_id,
            edit_of_event_id=edit_of,
        )

    def test_no_edits(self, store):
        self._create_msg("$orig1", text="original")
        assert store.reconcile_edits() == 0

    def test_simple_edit_replaces_text(self, store):
        self._create_msg("$orig1", text="original")
        self._create_msg("$edit1", direction="edit", text="edited", edit_of="$orig1")
        result = store.reconcile_edits()
        assert result == 1
        orig = Message.get(Message.event_id == "$orig1")
        assert orig.text == "edited"
        assert Message.select().where(Message.event_id == "$edit1").count() == 0

    def test_edit_of_nonexistent_promotes_to_forward(self, store):
        self._create_msg("$edit1", direction="edit", text="orphan edit", edit_of="$missing")
        result = store.reconcile_edits()
        assert result == 1
        edit = Message.get(Message.event_id == "$edit1")
        assert edit.direction == "forward"
        assert edit.edit_of_event_id == ""

    def test_latest_edit_wins(self, store):
        t1 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 2, tzinfo=timezone.utc)
        self._create_msg("$orig1", text="original")
        self._create_msg("$edit1", direction="edit", text="first edit", edit_of="$orig1", timestamp=t1)
        self._create_msg("$edit2", direction="edit", text="second edit", edit_of="$orig1", timestamp=t2)
        result = store.reconcile_edits()
        assert result == 1
        orig = Message.get(Message.event_id == "$orig1")
        assert orig.text == "second edit"

    def test_multiple_edits_different_originals(self, store):
        t1 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self._create_msg("$orig1", text="o1")
        self._create_msg("$orig2", text="o2")
        self._create_msg("$edit1", direction="edit", text="e1", edit_of="$orig1", timestamp=t1)
        self._create_msg("$edit2", direction="edit", text="e2", edit_of="$orig2", timestamp=t2)
        result = store.reconcile_edits()
        assert result == 2
        assert Message.get(Message.event_id == "$orig1").text == "e1"
        assert Message.get(Message.event_id == "$orig2").text == "e2"

    def test_empty_database(self, store):
        assert store.reconcile_edits() == 0

    def test_duplicate_event_id_not_inserted(self, store):
        self._create_msg("$orig1", text="original")
        self._create_msg("$edit1", direction="edit", text="edited", edit_of="$orig1")
        store.reconcile_edits()
        result = store.reconcile_edits()
        assert result == 0
