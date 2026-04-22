from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional

from bridge.models import BridgeMessage


class BaseBackend(ABC):
    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self._message_callback: Optional[Callable[[BridgeMessage], Awaitable[None]]] = None

    def on_message(self, callback: Callable[[BridgeMessage], Awaitable[None]]) -> None:
        self._message_callback = callback

    async def _emit_message(self, message: BridgeMessage) -> None:
        if self._message_callback is not None:
            await self._message_callback(message)

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send_message(self, room_id: str, text: str, msgtype: str = "m.text") -> str:
        raise NotImplementedError

    @abstractmethod
    async def send_media(
        self,
        room_id: str,
        data: bytes,
        mimetype: str,
        filename: str,
        msgtype: str = "m.file",
        extra_info: Optional[dict] = None,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    async def redact_event(self, room_id: str, event_id: str, reason: Optional[str] = None) -> str:
        raise NotImplementedError

    @abstractmethod
    async def edit_message(self, room_id: str, event_id: str, new_text: str, msgtype: str = "m.notice") -> str:
        raise NotImplementedError

    @abstractmethod
    async def resolve_room_id(self, room_alias_or_id: str) -> Optional[str]:
        raise NotImplementedError
