from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class MessageDirection(str, Enum):
    FORWARD = "forward"
    REPLY = "reply"
    CONTROL = "control"
    REDACT = "redact"
    EDIT = "edit"


class MessageType(str, Enum):
    TEXT = "m.text"
    IMAGE = "m.image"
    VIDEO = "m.video"
    AUDIO = "m.audio"
    FILE = "m.file"
    NOTICE = "m.notice"
    EMOTE = "m.emote"
    CALL_NOTIFICATION = "call_notification"


class CallAction(str, Enum):
    STARTED = "started"
    ANSWERED = "answered"
    ENDED = "ended"


@dataclass
class BridgeMessage:
    source_room_id: str
    source_room_name: str
    sender: str
    sender_displayname: str
    text: str
    timestamp: datetime
    event_id: str
    backend_name: str
    direction: MessageDirection
    msgtype: MessageType = MessageType.TEXT

    target_room_id: Optional[str] = None
    target_room_name: Optional[str] = None

    media_url: Optional[str] = None
    media_data: Optional[bytes] = None
    media_mimetype: Optional[str] = None
    media_filename: Optional[str] = None
    media_size: Optional[int] = None
    thumbnail_url: Optional[str] = None

    call_type: Optional[str] = None
    call_action: Optional[CallAction] = None
    call_duration: Optional[int] = None
    call_callee: Optional[str] = None
    call_join_url: Optional[str] = None

    media_width: Optional[int] = None
    media_height: Optional[int] = None
    media_duration: Optional[int] = None

    from_self: bool = False
    edit_of_event_id: Optional[str] = None
    reply_to_event_id: Optional[str] = None
    redacted_event_id: Optional[str] = None
    extra_content: dict = field(default_factory=dict)
