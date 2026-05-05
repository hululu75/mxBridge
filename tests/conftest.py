from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import peewee
import pytest

from bridge.message_store import (
    ALL_MODELS,
    BridgeConfig,
    EventRoomMap,
    FailedDecryption,
    Message,
    MessageStore,
    ProcessedEvent,
    RoomAlias,
    SourceTargetMap,
    UserAlias,
    db,
)
from bridge.models import BridgeMessage, CallAction, MessageDirection, MessageType
from bridge.state import StateManager


@pytest.fixture(autouse=True)
def _setup_logging():
    import logging
    logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")


def _utcnow_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _make_msg(
    event_id: str = "$evt1",
    sender: str = "@alice:example.com",
    sender_displayname: str = "Alice",
    room_id: str = "!room1:example.com",
    room_name: str = "Test Room",
    text: str = "hello",
    direction: MessageDirection = MessageDirection.FORWARD,
    msgtype: MessageType = MessageType.TEXT,
    timestamp: datetime | None = None,
    edit_of_event_id: str = "",
    reply_to_event_id: str = "",
) -> BridgeMessage:
    return BridgeMessage(
        source_room_id=room_id,
        source_room_name=room_name,
        sender=sender,
        sender_displayname=sender_displayname,
        text=text,
        timestamp=timestamp or datetime.now(timezone.utc),
        event_id=event_id,
        backend_name="test",
        direction=direction,
        msgtype=msgtype,
        edit_of_event_id=edit_of_event_id,
        reply_to_event_id=reply_to_event_id,
    )


@pytest.fixture
def tmp_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    real_db = peewee.SqliteDatabase(db_path, pragmas={
        "journal_mode": "wal",
        "busy_timeout": 5000,
    })
    db.initialize(real_db)
    db.connect(reuse_if_open=True)
    db.create_tables(ALL_MODELS, safe=True)
    yield real_db
    db.close()
    real_db.close()


@pytest.fixture
def store(tmp_path, tmp_db):
    db_path = str(tmp_path / "test.db")
    s = MessageStore.__new__(MessageStore)
    s._path = db_path
    s._media_dir = ""
    s._fts_available = False
    s._encrypted = False
    s._real_db = tmp_db
    return s


@pytest.fixture
def state(tmp_db):
    s = StateManager.__new__(StateManager)
    s._json_path = "/nonexistent"
    s._sync_tokens = {}
    s._processed_set = set()
    s._event_room_map = {}
    s._source_target_map = {}
    s._forwarding_enabled = True
    s._forwarding_paused = False
    s._failed_decryptions = {}
    return s
