"""P1-4: save_message_with_aliases combines 3 operations into 1 call."""
from __future__ import annotations

from datetime import datetime, timezone

from bridge.message_store import Message, RoomAlias, UserAlias
from bridge.models import MessageDirection, MessageType
from tests.conftest import _make_msg


class TestSaveMessageWithAliases:
    def test_saves_message_and_user_alias(self, store):
        msg = _make_msg(
            event_id="$e1",
            sender="@alice:srv",
            sender_displayname="Alice Name",
        )
        store.save_message_with_aliases(msg)
        assert Message.select().where(Message.event_id == "$e1").count() == 1
        alias = UserAlias.get(UserAlias.sender_id == "@alice:srv")
        assert alias.displayname == "Alice Name"

    def test_saves_message_and_room_alias(self, store):
        msg = _make_msg(
            event_id="$e1",
            room_id="!room1:srv",
            room_name="Room One",
        )
        store.save_message_with_aliases(msg)
        assert Message.select().where(Message.event_id == "$e1").count() == 1
        alias = RoomAlias.get(RoomAlias.room_id == "!room1:srv")
        assert alias.room_name == "Room One"

    def test_skips_user_alias_when_same_as_sender(self, store):
        msg = _make_msg(
            event_id="$e1",
            sender="@alice:srv",
            sender_displayname="@alice:srv",
        )
        store.save_message_with_aliases(msg)
        assert Message.select().where(Message.event_id == "$e1").count() == 1
        assert UserAlias.select().where(UserAlias.sender_id == "@alice:srv").count() == 0

    def test_skips_room_alias_when_same_as_room_id(self, store):
        msg = _make_msg(
            event_id="$e1",
            room_id="!room1:srv",
            room_name="!room1:srv",
        )
        store.save_message_with_aliases(msg)
        assert RoomAlias.select().where(RoomAlias.room_id == "!room1:srv").count() == 0

    def test_skips_aliases_when_empty(self, store):
        msg = _make_msg(
            event_id="$e1",
            sender_displayname="",
            room_name="",
        )
        store.save_message_with_aliases(msg)
        assert UserAlias.select().count() == 0
        assert RoomAlias.select().count() == 0

    def test_updates_existing_alias(self, store):
        UserAlias.insert(sender_id="@alice:srv", displayname="Old").execute()
        msg = _make_msg(
            event_id="$e1",
            sender="@alice:srv",
            sender_displayname="New",
        )
        store.save_message_with_aliases(msg)
        alias = UserAlias.get(UserAlias.sender_id == "@alice:srv")
        assert alias.displayname == "New"

    def test_no_event_id_skips(self, store):
        msg = _make_msg(event_id="")
        store.save_message_with_aliases(msg)
        assert Message.select().count() == 0

    def test_duplicate_event_id_does_not_raise(self, store):
        msg = _make_msg(event_id="$e1")
        store.save_message_with_aliases(msg)
        store.save_message_with_aliases(msg)
        assert Message.select().where(Message.event_id == "$e1").count() == 1
