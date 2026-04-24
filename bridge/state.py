from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import peewee

from bridge.message_store import (
    BridgeConfig,
    EventRoomMap,
    FailedDecryption,
    ProcessedEvent,
    SourceTargetMap,
)

logger = logging.getLogger(__name__)

MAX_PROCESSED_CACHE = 10000
MAX_EVENT_MAP = 5000
MAX_FAILED_DECRYPTIONS = 500


class StateManager:
    def __init__(self, json_path: str = "state.json"):
        self._json_path = os.path.abspath(json_path)
        self._sync_tokens: dict[str, str] = {}
        self._processed_set: set[str] = set()
        self._event_room_map: dict[str, str] = {}
        self._source_target_map: dict[str, str] = {}
        self._forwarding_enabled: bool = True
        self._forwarding_paused: bool = False
        self._failed_decryptions: dict[str, list[dict]] = {}

    async def load(self) -> None:
        await asyncio.to_thread(self._load_from_db)
        json_path = self._json_path
        if os.path.exists(json_path):
            await asyncio.to_thread(self._migrate_from_json, json_path)

    def _load_from_db(self) -> None:
        for row in BridgeConfig.select().where(
            BridgeConfig.key.startswith("sync_token:")
        ):
            name = row.key[len("sync_token:"):]
            self._sync_tokens[name] = row.value

        for row in BridgeConfig.select().where(
            BridgeConfig.key == "forwarding_enabled"
        ):
            self._forwarding_enabled = row.value == "1"
        for row in BridgeConfig.select().where(
            BridgeConfig.key == "forwarding_paused"
        ):
            self._forwarding_paused = row.value == "1"

        self._processed_set = set(
            row.event_id for row in ProcessedEvent.select()
        )

        self._event_room_map = {
            row.event_id: row.room_id for row in EventRoomMap.select()
        }

        self._source_target_map = {
            row.source_event_id: row.target_event_id
            for row in SourceTargetMap.select()
        }

        self._failed_decryptions = {}
        for row in FailedDecryption.select():
            self._failed_decryptions.setdefault(row.session_id, []).append(
                {"room_id": row.room_id, "event_id": row.event_id}
            )

    def _migrate_from_json(self, path: str) -> None:
        try:
            with open(path, "r") as f:
                data = json.loads(f.read())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read %s for migration: %s", path, e)
            return

        logger.info("Migrating %s -> SQLite ...", path)

        for name, token in data.get("sync_tokens", {}).items():
            BridgeConfig.replace(
                key=f"sync_token:{name}", value=token
            ).execute()

        forwarding_enabled = data.get("forwarding_enabled", True)
        BridgeConfig.replace(
            key="forwarding_enabled", value="1" if forwarding_enabled else "0"
        ).execute()

        forwarding_paused = data.get("forwarding_paused", False)
        BridgeConfig.replace(
            key="forwarding_paused", value="1" if forwarding_paused else "0"
        ).execute()

        processed = data.get("processed_events", [])[-MAX_PROCESSED_CACHE:]
        if processed:
            for eid in processed:
                ProcessedEvent.insert(event_id=eid).on_conflict_ignore().execute()
            self._processed_set.update(processed)

        event_room_map = data.get("event_room_map", {})
        if event_room_map:
            for eid, rid in event_room_map.items():
                EventRoomMap.replace(event_id=eid, room_id=rid).execute()
            self._event_room_map.update(event_room_map)

        source_target_map = data.get("source_target_map", {})
        if source_target_map:
            for sid, tid in source_target_map.items():
                SourceTargetMap.replace(source_event_id=sid, target_event_id=tid).execute()
            self._source_target_map.update(source_target_map)

        failed = data.get("failed_decryptions", {})
        if failed:
            for sid, events in failed.items():
                for ev in events:
                    FailedDecryption.insert(
                        session_id=sid, room_id=ev["room_id"], event_id=ev["event_id"]
                    ).on_conflict_ignore().execute()
            self._failed_decryptions = failed

        self._sync_tokens = data.get("sync_tokens", {})
        self._forwarding_enabled = forwarding_enabled
        self._forwarding_paused = forwarding_paused

        os.remove(path)
        logger.info("Migrated %s to SQLite, file deleted", path)

    # -------------------------------------------------- sync tokens

    async def save_sync_token(self, backend_name: str, token: str) -> None:
        self._sync_tokens[backend_name] = token
        await asyncio.to_thread(
            lambda: BridgeConfig.replace(
                key=f"sync_token:{backend_name}", value=token
            ).execute()
        )

    def load_sync_token(self, backend_name: str) -> Optional[str]:
        return self._sync_tokens.get(backend_name)

    # -------------------------------------------------- event-room map

    async def save_event_room(self, event_id: str, room_id: str) -> None:
        self._event_room_map[event_id] = room_id
        await asyncio.to_thread(self._trim_and_save_event_room, event_id, room_id)

    def _trim_and_save_event_room(self, event_id: str, room_id: str) -> None:
        EventRoomMap.replace(event_id=event_id, room_id=room_id).execute()
        count = EventRoomMap.select().count()
        if count > MAX_EVENT_MAP:
            excess = count - MAX_EVENT_MAP
            removed = [r.event_id for r in EventRoomMap.select(EventRoomMap.event_id).order_by(peewee.SQL('rowid').asc()).limit(excess)]
            EventRoomMap.delete().where(EventRoomMap.event_id << removed).execute()
            for eid in removed:
                self._event_room_map.pop(eid, None)

    def get_event_room(self, event_id: str) -> Optional[str]:
        return self._event_room_map.get(event_id)

    # -------------------------------------------------- source-target map

    async def save_source_target(self, source_event_id: str, target_event_id: str) -> None:
        self._source_target_map[source_event_id] = target_event_id
        await asyncio.to_thread(self._trim_and_save_source_target, source_event_id, target_event_id)

    def _trim_and_save_source_target(self, source_event_id: str, target_event_id: str) -> None:
        SourceTargetMap.replace(source_event_id=source_event_id, target_event_id=target_event_id).execute()
        count = SourceTargetMap.select().count()
        if count > MAX_EVENT_MAP:
            excess = count - MAX_EVENT_MAP
            removed = [r.source_event_id for r in SourceTargetMap.select(SourceTargetMap.source_event_id).order_by(peewee.SQL('rowid').asc()).limit(excess)]
            SourceTargetMap.delete().where(SourceTargetMap.source_event_id << removed).execute()
            for sid in removed:
                self._source_target_map.pop(sid, None)

    def get_target_event_id(self, source_event_id: str) -> Optional[str]:
        return self._source_target_map.get(source_event_id)

    async def pop_source_target(self, source_event_id: str) -> Optional[str]:
        val = self._source_target_map.pop(source_event_id, None)
        if val:
            await asyncio.to_thread(
                lambda: SourceTargetMap.delete().where(
                    SourceTargetMap.source_event_id == source_event_id
                ).execute()
            )
        return val

    async def clear_mappings(self) -> None:
        self._event_room_map.clear()
        self._source_target_map.clear()
        await asyncio.to_thread(self._clear_mappings_db)

    def _clear_mappings_db(self) -> None:
        try:
            EventRoomMap.delete().execute()
            SourceTargetMap.delete().execute()
        except Exception:
            pass

    # -------------------------------------------------- processed events

    def is_processed(self, event_id: str) -> bool:
        return event_id in self._processed_set

    async def mark_processed(self, event_id: str) -> None:
        if event_id in self._processed_set:
            return
        self._processed_set.add(event_id)
        await asyncio.to_thread(self._trim_and_save_processed, event_id)

    def _trim_and_save_processed(self, event_id: str) -> None:
        ProcessedEvent.insert(event_id=event_id).on_conflict_ignore().execute()
        count = ProcessedEvent.select().count()
        if count > MAX_PROCESSED_CACHE:
            excess = count - MAX_PROCESSED_CACHE
            removed = [r.event_id for r in ProcessedEvent.select(ProcessedEvent.event_id).order_by(peewee.SQL('rowid').asc()).limit(excess)]
            ProcessedEvent.delete().where(ProcessedEvent.event_id << removed).execute()
            self._processed_set -= set(removed)

    # -------------------------------------------------- forwarding state

    def get_forwarding_enabled(self) -> bool:
        return self._forwarding_enabled

    async def set_forwarding_enabled(self, enabled: bool) -> None:
        self._forwarding_enabled = enabled
        await asyncio.to_thread(
            lambda: BridgeConfig.replace(
                key="forwarding_enabled", value="1" if enabled else "0"
            ).execute()
        )

    async def set_forwarding_paused(self, paused: bool) -> None:
        self._forwarding_paused = paused
        await asyncio.to_thread(
            lambda: BridgeConfig.replace(
                key="forwarding_paused", value="1" if paused else "0"
            ).execute()
        )

    def get_forwarding_paused(self) -> bool:
        return self._forwarding_paused

    # -------------------------------------------------- failed decryptions

    async def save_failed_decryption(self, session_id: str, room_id: str, event_id: str) -> None:
        events = self._failed_decryptions.setdefault(session_id, [])
        if any(e["event_id"] == event_id for e in events):
            return
        events.append({"room_id": room_id, "event_id": event_id})
        await asyncio.to_thread(self._trim_and_save_failed, session_id, room_id, event_id)

    def _trim_and_save_failed(self, session_id: str, room_id: str, event_id: str) -> None:
        try:
            FailedDecryption.insert(
                session_id=session_id, room_id=room_id, event_id=event_id
            ).on_conflict_ignore().execute()
        except Exception:
            pass
        count = FailedDecryption.select().count()
        if count > MAX_FAILED_DECRYPTIONS:
            excess = count - MAX_FAILED_DECRYPTIONS
            oldest = list(FailedDecryption.select(FailedDecryption.session_id, FailedDecryption.event_id).order_by(peewee.SQL('rowid').asc()).limit(excess))
            if oldest:
                removed_sessions: dict[str, set[str]] = {}
                for r in oldest:
                    removed_sessions.setdefault(r.session_id, set()).add(r.event_id)
                all_pairs = [(r.session_id, r.event_id) for r in oldest]
                for sid, eid in all_pairs:
                    FailedDecryption.delete().where(
                        (FailedDecryption.session_id == sid) & (FailedDecryption.event_id == eid)
                    ).execute()
                for sid, eids in removed_sessions.items():
                    session_events = self._failed_decryptions.get(sid)
                    if session_events:
                        remaining = [e for e in session_events if e["event_id"] not in eids]
                        if remaining:
                            self._failed_decryptions[sid] = remaining
                        else:
                            self._failed_decryptions.pop(sid, None)

    async def pop_failed_decryptions(self, session_id: str) -> list[dict]:
        items = self._failed_decryptions.pop(session_id, [])
        if items:
            await asyncio.to_thread(
                lambda: FailedDecryption.delete().where(
                    FailedDecryption.session_id == session_id
                ).execute()
            )
        return items

    def get_failed_decryption_sessions(self) -> list[str]:
        return list(self._failed_decryptions.keys())

    # -------------------------------------------------- flush (no-op for SQLite)

    async def flush(self) -> None:
        pass
