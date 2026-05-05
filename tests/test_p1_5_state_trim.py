"""P1-5: state trim methods use single COUNT and select only needed columns."""
from __future__ import annotations

from bridge.message_store import EventRoomMap, ProcessedEvent, SourceTargetMap, db
from bridge.state import MAX_EVENT_MAP, MAX_PROCESSED_CACHE, StateManager


class TestTrimEventRoom:
    def test_trim_removes_oldest(self, state):
        for i in range(MAX_EVENT_MAP + 10):
            state._trim_and_save_event_room(f"$e{i}", f"!room{i}:srv")
        assert EventRoomMap.select().count() <= MAX_EVENT_MAP
        db_entries = {r.event_id for r in EventRoomMap.select()}
        assert "$e0" not in db_entries
        assert f"$e{MAX_EVENT_MAP + 9}" in db_entries

    def test_no_trim_when_under_limit(self, state):
        state._trim_and_save_event_room("$e1", "!r1:srv")
        assert EventRoomMap.select().count() == 1
        assert EventRoomMap.get(EventRoomMap.event_id == "$e1").room_id == "!r1:srv"


class TestTrimSourceTarget:
    def test_trim_removes_oldest(self, state):
        for i in range(MAX_EVENT_MAP + 10):
            state._trim_and_save_source_target(f"$src{i}", f"$tgt{i}")
        assert SourceTargetMap.select().count() <= MAX_EVENT_MAP
        db_entries = {r.source_event_id for r in SourceTargetMap.select()}
        assert "$src0" not in db_entries
        assert f"$src{MAX_EVENT_MAP + 9}" in db_entries


class TestTrimProcessed:
    def test_trim_removes_oldest(self, state):
        for i in range(MAX_PROCESSED_CACHE + 10):
            state._trim_and_save_processed(f"$evt{i}")
        assert ProcessedEvent.select().count() <= MAX_PROCESSED_CACHE
        db_entries = {r.event_id for r in ProcessedEvent.select()}
        assert "$evt0" not in db_entries
        assert f"$evt{MAX_PROCESSED_CACHE + 9}" in db_entries

    def test_duplicate_processed_not_inserted(self, state):
        state._trim_and_save_processed("$e1")
        state._trim_and_save_processed("$e1")
        assert ProcessedEvent.select().count() == 1


class TestTrimFailedDecryption:
    def test_trim_removes_oldest(self, state):
        from bridge.state import MAX_FAILED_DECRYPTIONS
        for i in range(MAX_FAILED_DECRYPTIONS + 10):
            state._failed_decryptions.setdefault(f"sess{i%3}", []).append(
                {"room_id": f"!r{i}:srv", "event_id": f"$e{i}"}
            )
        state._trim_and_save_failed("sess_new", "!r:srv", "$enew")
        assert db.execute_sql("SELECT COUNT(*) FROM state_failed_decryptions").fetchone()[0] <= MAX_FAILED_DECRYPTIONS
