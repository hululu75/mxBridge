"""P1-1: _enrich_aliases uses WHERE IN instead of full table scan."""
from __future__ import annotations

from datetime import datetime, timezone

from bridge.message_store import Message, RoomAlias, UserAlias, db


class TestEnrichAliasesWhereIn:
    def test_empty_results_returns_empty(self, store):
        assert store._enrich_aliases([]) == []

    def test_no_aliases_tables_exist(self, store):
        Message.create(
            timestamp=datetime.now(timezone.utc),
            direction="forward",
            source_room_id="!room1:srv",
            source_room_name="",
            sender="@alice:srv",
            sender_displayname="",
            text="hi",
            msgtype="m.text",
            event_id="$evt1",
        )
        results = [store._model_to_dict(Message.get_by_id(1))]
        enriched = store._enrich_aliases(results)
        assert enriched[0]["sender_displayname"] == ""

    def test_aliases_populated_only_for_matching_ids(self, store):
        UserAlias.insert(sender_id="@alice:srv", displayname="Alice Alias").execute()
        UserAlias.insert(sender_id="@bob:srv", displayname="Bob Alias").execute()
        RoomAlias.insert(room_id="!room1:srv", room_name="Room One").execute()
        RoomAlias.insert(room_id="!room2:srv", room_name="Room Two").execute()

        Message.create(
            timestamp=datetime.now(timezone.utc),
            direction="forward",
            source_room_id="!room1:srv",
            source_room_name="",
            sender="@alice:srv",
            sender_displayname="",
            text="hi",
            msgtype="m.text",
            event_id="$evt1",
        )
        results = [store._model_to_dict(Message.get_by_id(1))]
        enriched = store._enrich_aliases(results)
        assert enriched[0]["sender_displayname"] == "Alice Alias"
        assert enriched[0]["source_room_name"] == "Room One"

    def test_does_not_overwrite_existing_displayname(self, store):
        UserAlias.insert(sender_id="@alice:srv", displayname="New Name").execute()
        Message.create(
            timestamp=datetime.now(timezone.utc),
            direction="forward",
            source_room_id="!room1:srv",
            source_room_name="Room",
            sender="@alice:srv",
            sender_displayname="Alice Original",
            text="hi",
            msgtype="m.text",
            event_id="$evt1",
        )
        results = [store._model_to_dict(Message.get_by_id(1))]
        enriched = store._enrich_aliases(results)
        assert enriched[0]["sender_displayname"] == "Alice Original"

    def test_overwrites_when_displayname_equals_sender(self, store):
        UserAlias.insert(sender_id="@alice:srv", displayname="Alice Alias").execute()
        Message.create(
            timestamp=datetime.now(timezone.utc),
            direction="forward",
            source_room_id="!room1:srv",
            source_room_name="Room",
            sender="@alice:srv",
            sender_displayname="@alice:srv",
            text="hi",
            msgtype="m.text",
            event_id="$evt1",
        )
        results = [store._model_to_dict(Message.get_by_id(1))]
        enriched = store._enrich_aliases(results)
        assert enriched[0]["sender_displayname"] == "Alice Alias"

    def test_only_queries_needed_senders(self, store):
        UserAlias.insert(sender_id="@alice:srv", displayname="Alice").execute()
        UserAlias.insert(sender_id="@bob:srv", displayname="Bob").execute()

        Message.create(
            timestamp=datetime.now(timezone.utc),
            direction="forward",
            source_room_id="!room1:srv",
            source_room_name="Room",
            sender="@charlie:srv",
            sender_displayname="Charlie",
            text="hi",
            msgtype="m.text",
            event_id="$evt1",
        )
        results = [store._model_to_dict(Message.get_by_id(1))]
        enriched = store._enrich_aliases(results)
        assert enriched[0]["sender_displayname"] == "Charlie"

    def test_multiple_results_mixed_enrichment(self, store):
        UserAlias.insert(sender_id="@alice:srv", displayname="Alice A").execute()
        RoomAlias.insert(room_id="!room1:srv", room_name="Room One").execute()

        Message.create(
            timestamp=datetime.now(timezone.utc), direction="forward",
            source_room_id="!room1:srv", source_room_name="",
            sender="@alice:srv", sender_displayname="",
            text="msg1", msgtype="m.text", event_id="$e1",
        )
        Message.create(
            timestamp=datetime.now(timezone.utc), direction="forward",
            source_room_id="!room2:srv", source_room_name="!room2:srv",
            sender="@bob:srv", sender_displayname="Bob",
            text="msg2", msgtype="m.text", event_id="$e2",
        )
        results = [store._model_to_dict(Message.get_by_id(i)) for i in (1, 2)]
        enriched = store._enrich_aliases(results)
        assert enriched[0]["sender_displayname"] == "Alice A"
        assert enriched[0]["source_room_name"] == "Room One"
        assert enriched[1]["sender_displayname"] == "Bob"
        assert enriched[1]["source_room_name"] == "!room2:srv"
