from __future__ import annotations

import asyncio
import json
import os
import stat
from typing import Optional

import aiofiles

MAX_PROCESSED_CACHE = 10000
MAX_EVENT_MAP = 5000
MAX_FAILED_DECRYPTIONS = 500


class StateManager:
    def __init__(self, path: str = "state.json"):
        self._path = os.path.abspath(path)
        self._sync_tokens: dict[str, str] = {}
        self._processed_events: list[str] = []
        self._processed_set: set[str] = set()
        self._event_room_map: dict[str, str] = {}
        self._source_target_map: dict[str, str] = {}
        self._forwarding_enabled: bool = True
        self._forwarding_paused: bool = False
        self._failed_decryptions: dict[str, list[dict]] = {}
        self._dirty = False
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            async with aiofiles.open(self._path, "r") as f:
                data = json.loads(await f.read())
            self._sync_tokens = data.get("sync_tokens", {})
            self._processed_events = data.get("processed_events", [])
            self._processed_set = set(self._processed_events)
            self._forwarding_enabled = data.get("forwarding_enabled", True)
            self._forwarding_paused = data.get("forwarding_paused", False)
            self._failed_decryptions = data.get("failed_decryptions", {})
            self._event_room_map = data.get("event_room_map", {})
            self._source_target_map = data.get("source_target_map", {})
        except (json.JSONDecodeError, OSError):
            self._sync_tokens = {}
            self._processed_events = []
            self._processed_set = set()
            self._forwarding_enabled = True
            self._forwarding_paused = False
            self._event_room_map = {}
            self._source_target_map = {}
            self._failed_decryptions = {}

    async def save(self) -> None:
        async with self._lock:
            await self._do_save()

    async def _do_save(self) -> None:
        data = {
            "sync_tokens": self._sync_tokens,
            "processed_events": self._processed_events[-MAX_PROCESSED_CACHE:],
            "forwarding_enabled": self._forwarding_enabled,
            "forwarding_paused": self._forwarding_paused,
            "failed_decryptions": self._failed_decryptions,
            "event_room_map": self._event_room_map,
            "source_target_map": self._source_target_map,
        }
        tmp_path = self._path + ".tmp"
        async with aiofiles.open(tmp_path, "w") as f:
            await f.write(json.dumps(data, indent=2))
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, self._path)

    async def save_sync_token(self, backend_name: str, token: str) -> None:
        self._sync_tokens[backend_name] = token
        self._dirty = True

    def load_sync_token(self, backend_name: str) -> Optional[str]:
        return self._sync_tokens.get(backend_name)

    async def save_event_room(self, event_id: str, room_id: str) -> None:
        self._event_room_map[event_id] = room_id
        if len(self._event_room_map) > MAX_EVENT_MAP:
            keys = list(self._event_room_map.keys())
            for k in keys[: len(keys) - MAX_EVENT_MAP]:
                del self._event_room_map[k]
        self._dirty = True

    def get_event_room(self, event_id: str) -> Optional[str]:
        return self._event_room_map.get(event_id)

    async def save_source_target(self, source_event_id: str, target_event_id: str) -> None:
        self._source_target_map[source_event_id] = target_event_id
        if len(self._source_target_map) > MAX_EVENT_MAP:
            keys = list(self._source_target_map.keys())
            for k in keys[: len(keys) - MAX_EVENT_MAP]:
                del self._source_target_map[k]
        self._dirty = True

    def get_target_event_id(self, source_event_id: str) -> Optional[str]:
        return self._source_target_map.get(source_event_id)

    def pop_source_target(self, source_event_id: str) -> Optional[str]:
        return self._source_target_map.pop(source_event_id, None)

    def clear_mappings(self) -> None:
        self._event_room_map.clear()
        self._source_target_map.clear()
        self._dirty = True

    def is_processed(self, event_id: str) -> bool:
        return event_id in self._processed_set

    async def mark_processed(self, event_id: str) -> None:
        if event_id in self._processed_set:
            return
        self._processed_events.append(event_id)
        self._processed_set.add(event_id)
        if len(self._processed_events) > MAX_PROCESSED_CACHE:
            removed = self._processed_events[: len(self._processed_events) - MAX_PROCESSED_CACHE]
            self._processed_events = self._processed_events[-MAX_PROCESSED_CACHE:]
            self._processed_set -= set(removed)
        self._dirty = True

    def get_forwarding_enabled(self) -> bool:
        return self._forwarding_enabled

    async def set_forwarding_enabled(self, enabled: bool) -> None:
        self._forwarding_enabled = enabled
        self._dirty = True

    async def set_forwarding_paused(self, paused: bool) -> None:
        self._forwarding_paused = paused
        self._dirty = True

    def get_forwarding_paused(self) -> bool:
        return self._forwarding_paused

    async def save_failed_decryption(self, session_id: str, room_id: str, event_id: str) -> None:
        events = self._failed_decryptions.setdefault(session_id, [])
        if any(e["event_id"] == event_id for e in events):
            return
        events.append({"room_id": room_id, "event_id": event_id})
        total = sum(len(v) for v in self._failed_decryptions.values())
        if total > MAX_FAILED_DECRYPTIONS:
            oldest = next(iter(self._failed_decryptions))
            del self._failed_decryptions[oldest]
        self._dirty = True

    def pop_failed_decryptions(self, session_id: str) -> list[dict]:
        items = self._failed_decryptions.pop(session_id, [])
        if items:
            self._dirty = True
        return items

    def get_failed_decryption_sessions(self) -> list[str]:
        return list(self._failed_decryptions.keys())

    async def flush(self) -> None:
        if self._dirty:
            await self.save()
            self._dirty = False
