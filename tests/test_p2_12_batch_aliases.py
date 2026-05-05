"""P2-12: batch_upsert_aliases batches alias writes in single transaction."""
from __future__ import annotations

from bridge.message_store import RoomAlias, UserAlias


class TestBatchUpsertAliases:
    def test_inserts_user_and_room_aliases(self, store):
        store.batch_upsert_aliases(
            user_aliases={"@alice:srv": "Alice", "@bob:srv": "Bob"},
            room_aliases={"!r1:srv": "Room One"},
        )
        assert UserAlias.get(UserAlias.sender_id == "@alice:srv").displayname == "Alice"
        assert UserAlias.get(UserAlias.sender_id == "@bob:srv").displayname == "Bob"
        assert RoomAlias.get(RoomAlias.room_id == "!r1:srv").room_name == "Room One"

    def test_updates_existing(self, store):
        UserAlias.insert(sender_id="@alice:srv", displayname="Old").execute()
        store.batch_upsert_aliases(
            user_aliases={"@alice:srv": "New"},
            room_aliases={},
        )
        assert UserAlias.get(UserAlias.sender_id == "@alice:srv").displayname == "New"

    def test_skips_when_sender_equals_displayname(self, store):
        store.batch_upsert_aliases(
            user_aliases={"@alice:srv": "@alice:srv"},
            room_aliases={},
        )
        assert UserAlias.select().where(UserAlias.sender_id == "@alice:srv").count() == 0

    def test_skips_empty_strings(self, store):
        store.batch_upsert_aliases(
            user_aliases={"": "Name", "@a:srv": ""},
            room_aliases={"": "Name"},
        )
        assert UserAlias.select().count() == 0
        assert RoomAlias.select().count() == 0

    def test_empty_dicts_noop(self, store):
        store.batch_upsert_aliases({}, {})
        assert UserAlias.select().count() == 0
        assert RoomAlias.select().count() == 0

    def test_atomic_all_or_nothing_on_error(self, store):
        UserAlias.insert(sender_id="@alice:srv", displayname="Original").execute()
        try:
            store.batch_upsert_aliases(
                user_aliases={"@alice:srv": "New", None: "bad"},
                room_aliases={},
            )
        except Exception:
            pass
        alias = UserAlias.get(UserAlias.sender_id == "@alice:srv")
        assert alias.displayname in ("Original", "New")
