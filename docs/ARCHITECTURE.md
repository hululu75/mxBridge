# Matrix Bridge — Technical Architecture

## 1. System Overview

### Bridge Mode

```
┌──────────────────┐         ┌─────────────────┐         ┌──────────────────┐
│   Matrix Server A│         │   Bridge Core    │         │  Matrix Server B │
│                  │         │                  │         │                  │
│  ┌────────────┐  │  sync   │  ┌────────────┐  │  sync   │  ┌────────────┐  │
│  │  Rooms     │◄─┼─────────┼─►│  Source     │  │────────►│  │ Aggregation │  │
│  │  (many)    │  │         │  │  Backend    │  │         │  │ Room (one)  │  │
│  └────────────┘  │         │  └──────┬─────┘  │         │  └──────┬─────┘  │
│                  │         │         │        │         │         │        │
│                  │         │  ┌──────▼─────┐  │         │  ┌──────▼─────┐  │
│                  │         │  │ Target     │  │         │  │  Target    │  │
│                  │         │  │ Backend    │  │         │  │  Backend   │  │
│                  │         │  └────────────┘  │         │  └────────────┘  │
└──────────────────┘         └─────────────────┘         └──────────────────┘
                                       │
                        ┌──────────────┼──────────────┐
                        │              │              │
                 ┌──────┴──────┐ ┌─────┴──────┐ ┌─────┴──────┐
                 │ State Store │ │ Message    │ │ Web Server │
                 │ state.json  │ │ Store      │ │ (aiohttp)  │
                 └─────────────┘ │ SQLite DB  │ └────────────┘
                                 └────────────┘
```

### Backup Mode (no target server)

```
┌──────────────────┐         ┌─────────────────┐
│   Matrix Server  │         │   Bridge Core    │
│                  │  sync   │  (backup mode)   │
│  ┌────────────┐  │◄────────┼─►│  Source     │  │
│  │  Rooms     │  │         │  │  Backend    │  │
│  │  (many)    │  │         │  └──────┬─────┘  │
│  └────────────┘  │         │         │        │
└──────────────────┘         │         ▼        │
                             │  ┌────────────┐  │
                             │  │ Message    │  │
                             │  │ Store      │  │
                             │  │ SQLite DB  │  │
                             │  └────────────┘  │
                             └─────────────────┘
```

The bridge runs as a long-lived process connecting to one or two Matrix servers as independent clients. No server-side modifications are required.

Two operating modes are supported:
- **Bridge mode** (default): forwards messages from source to target server, with optional reverse replies and control commands.
- **Backup mode**: when `target` section is absent from config, saves all messages and media to local SQLite storage without forwarding.

---

## 2. Project Structure

```
matrix/
├── main.py                       # Entry point: config loading, encryption, signal handling, startup
├── config.example.yaml           # Configuration template
├── requirements.txt              # Python dependencies
├── docs/
│   ├── ARCHITECTURE.md
│   ├── ARCHITECTURE_CN.md
│   ├── USERGUIDE.md
│   └── USERGUIDE_CN.md
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yaml
├── scripts/
│   ├── backfill.py               # CLI: import historical messages into MessageStore
│   ├── repair_media.py           # CLI: repair corrupted encrypted media files
│   └── encrypt_tool.py           # CLI: encrypt/decrypt config values
├── bridge/
│   ├── __init__.py
│   ├── models.py                 # BridgeMessage dataclass — unified cross-backend message model
│   ├── core.py                   # BridgeCore — message routing, backup mode, control commands
│   ├── state.py                  # StateManager — sync tokens, event dedup, forwarding state, event maps
│   ├── message_store.py          # MessageStore — SQLite-backed message persistence with FTS5
│   ├── web.py                    # WebServer — aiohttp HTTP API for message search and browsing
│   ├── crypto.py                 # Config field encryption/decryption (Fernet + PBKDF2)
│   └── templates/
│       ├── index.html            # Web UI single-page application
│       └── marked.min.js         # Markdown rendering library for web UI
├── backends/
│   ├── __init__.py
│   ├── base.py                   # BaseBackend — abstract interface for all protocol adapters
│   ├── matrix_base.py            # MatrixBackend — shared Matrix client logic (auth, sync, media, keys)
│   ├── matrix_source.py          # MatrixSourceBackend — monitors all rooms, emits FORWARD/EDIT/REDACT
│   └── matrix_target.py          # MatrixTargetBackend — aggregation room, reply-to, control commands
└── store/                        # Runtime E2EE key storage (auto-created)
    ├── source/                   # Olm/Megolm keys for Server A connection
    └── target/                   # Olm/Megolm keys for Server B connection
```

---

## 3. Module Specifications

### 3.1 `bridge/models.py` — Unified Message Model

A single data class representing any message regardless of source protocol.

```python
class MessageDirection(str, Enum):
    FORWARD = "forward"       # A → B message
    REPLY = "reply"           # B → A reply (via !send command or reply-to)
    CONTROL = "control"       # Bridge control command (!login, !logout, etc.)
    REDACT = "redact"         # Redaction event from source
    EDIT = "edit"             # Edit event from source

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
    # Identity
    source_room_id: str
    source_room_name: str
    sender: str
    sender_displayname: str
    text: str
    event_id: str
    timestamp: datetime
    backend_name: str
    direction: MessageDirection
    msgtype: MessageType

    # Routing
    target_room_id: str | None
    target_room_name: str | None

    # Media fields
    media_url: str | None
    media_data: bytes | None
    media_mimetype: str | None
    media_filename: str | None
    media_size: int | None
    thumbnail_url: str | None
    media_width / media_height / media_duration: int | None

    # Call notification fields
    call_type: str | None         # "voice" | "video"
    call_action: CallAction | None  # STARTED | ANSWERED | ENDED
    call_duration: int | None
    call_callee: str | None
    call_join_url: str | None

    # Edit / Redaction / Reply tracking
    from_self: bool                 # True if sender is the bridge bot itself
    edit_of_event_id: str | None   # References the original event being edited
    reply_to_event_id: str | None  # Matrix reply-to event ID
    redacted_event_id: str | None  # Event ID being redacted

    # Extensibility
    extra_content: dict
```

---

### 3.2 `backends/base.py` — Abstract Backend Interface

All protocol adapters must implement this interface:

```python
class BaseBackend(ABC):
    # Lifecycle
    async def start(self) -> None
    async def stop(self) -> None

    # Sending
    async def send_message(room_id, text, msgtype) -> str
    async def send_media(room_id, data, mimetype, filename, msgtype, extra_info) -> str
    async def redact_event(room_id, event_id, reason) -> str
    async def edit_message(room_id, event_id, new_text, msgtype) -> str
    async def resolve_room_id(room_alias_or_id) -> str | None

    # Event emission
    def on_message(callback)
    async def _emit_message(message)
```

**To add a new protocol** (e.g., Telegram, Discord, Teams):
1. Create `backends/telegram.py` inheriting `BaseBackend`
2. Implement all abstract methods
3. In `start()`, call `self._emit_message(BridgeMessage(...))` when a message is received
4. Add a `type: "telegram"` entry in `config.yaml`
5. Update `main.py` to instantiate the correct backend based on `type`

---

### 3.3 `backends/matrix_base.py` — Shared Matrix Client Logic

`MatrixBackend(BaseBackend)` contains all shared Matrix client logic used by both source and target backends. It was extracted to avoid duplication between `matrix_source.py` and `matrix_target.py`.

#### Key Components

| Component | Description |
|-----------|-------------|
| `_init_client()` | Create `AsyncClient`, authenticate (token or password), upload keys, import keys, verify connection |
| `_sync_loop()` | Long-poll sync with automatic key maintenance and to-device message flushing |
| `_download_media()` | Download media from `mxc://` URI with size limit and E2EE decryption support |
| `_import_keys_if_configured()` | Import Megolm session keys from an export file (Element key export) |
| `_persist_device_id()` | Write server-assigned `device_id` back to config YAML file |
| `_register_common_callbacks()` | Register SAS key verification auto-acceptance and room key listeners |
| `_enqueue_pending_encrypted()` | Queue failed decryptions for automatic retry when keys arrive |
| `_recheck_pending_keys()` | Periodically re-request missing room keys for queued events |

#### Authentication Flow

```
_init_client()
    │
    ├─ access_token provided? ──► restore_login()
    │
    └─ No token ──► Login with password
                     │
                     ├─ Server assigns device_id? ──► _persist_device_id()
                     │
                     └─ keys_upload() → keys_query() → _import_keys_if_configured()
```

#### Pending Encrypted Event Queue

When a Megolm event cannot be decrypted (missing session key), the event is queued:

1. Event + room stored in `_pending_encrypted[session_id]`
2. Room key is immediately requested via `request_room_key()`
3. Sender's device keys are queried and claimed
4. Background task (`_periodic_key_upload`) re-requests keys every 120 seconds
5. When a room key arrives (`_on_room_key_received`), queued events are decrypted and dispatched
6. Persisted across restarts via `StateManager._failed_decryptions`

Maximum queue size: 200 sessions (`MAX_PENDING_SESSIONS`).

#### SAS Key Verification

The bridge automatically accepts and completes interactive SAS key verification requests from other users:

```
KeyVerificationStart ──► accept_key_verification()
KeyVerificationKey    ──► confirm_short_auth_string()
KeyVerificationMac    ──► Mark as verified, send m.key.verification.done
```

Also handles `m.key.verification.request` to-device events by responding with `m.key.verification.ready`.

---

### 3.4 `backends/matrix_source.py` — Server A Backend

**Responsibility:** Connect to Server A, monitor all joined rooms, emit `FORWARD`, `EDIT`, and `REDACT` messages.

#### Initialization (`start()`)

1. Call `_init_client()` for authentication and key setup
2. Restore sync position from `StateManager`
3. Perform initial sync with `full_state=True` to load room state
4. Register event callbacks: `RoomMessage`, `CallInviteEvent`, `CallAnswerEvent`, `CallHangupEvent`, `MegolmEvent`, `RedactionEvent`
5. Query device keys for all members of encrypted rooms
6. Load failed decryption sessions from previous run
7. Start background tasks: periodic flush, key upload, call cleanup, sync loop

#### Event Processing Pipeline

```
Incoming event (sync response)
    │
    ├─ Is event_id processed? ──► SKIP (dedup)
    │
    ├─ RoomMessageText ──► Check m.relates_to
    │     ├─ rel_type == "m.replace" ──► BridgeMessage(direction=EDIT, edit_of_event_id=...)
    │     └─ Normal text ──► BridgeMessage(direction=FORWARD, msgtype=TEXT)
    │
    ├─ RoomMessageNotice ──► BridgeMessage(direction=FORWARD, msgtype=NOTICE)
    ├─ RoomMessageEmote ──► BridgeMessage(direction=FORWARD, msgtype=EMOTE)
    ├─ RoomMessageImage ──► download media ──► BridgeMessage(direction=FORWARD, msgtype=IMAGE)
    ├─ RoomMessageVideo ──► download media ──► BridgeMessage(direction=FORWARD, msgtype=VIDEO)
    ├─ RoomMessageAudio ──► download media ──► BridgeMessage(direction=FORWARD, msgtype=AUDIO)
    ├─ RoomMessageFile ──► download media ──► BridgeMessage(direction=FORWARD, msgtype=FILE)
    │
    ├─ RedactionEvent ──► BridgeMessage(direction=REDACT, redacted_event_id=...)
    │
    ├─ CallInviteEvent ──► parse SDP ──► BridgeMessage(msgtype=CALL_NOTIFICATION, call_action=STARTED)
    ├─ CallAnswerEvent ──► BridgeMessage(msgtype=CALL_NOTIFICATION, call_action=ANSWERED)
    ├─ CallHangupEvent ──► BridgeMessage(msgtype=CALL_NOTIFICATION, call_action=ENDED)
    │
    └─ MegolmEvent ──► decrypt
          ├─ Success ──► re-dispatch decrypted event through pipeline
          └─ Failure ──► _enqueue_pending_encrypted() (will retry when key arrives)
```

#### Edit Detection

Messages with `m.relates_to.rel_type == "m.replace"` and `m.new_content` are detected as edits. The edited text is extracted from `m.new_content.body` and emitted as `direction=EDIT` with `edit_of_event_id` referencing the original event.

#### Redaction Handling

`RedactionEvent` events from other users (not the bridge itself) are emitted as `direction=REDACT` with `redacted_event_id` set to the event being redacted.

#### Room Key Retry (Source-specific)

Overrides `_before_key_rerequest()` to call `cancel_key_share()` before re-requesting, and `_on_pending_encrypted_enqueued()` to persist failed decryptions to `StateManager`.

On room key arrival, retries both in-memory pending events and persisted events from previous runs (via `room_get_event` → decrypt → dispatch).

#### Call Detection

- `CallInviteEvent`: Inspects SDP offer for `"video"` keyword to classify as voice/video
- Tracks active calls in `_active_calls` dict (keyed by `call_id`)
- `CallHangupEvent`: Populates call duration from tracked state
- Stale calls (older than 24 hours) are cleaned up hourly

#### Media Download

- Downloads via `client.download(mxc=...)`
- For encrypted media (`RoomEncryptedMedia`), decrypts with `decrypt_attachment()`
- Respects `media_max_size` config (default 50 MB)
- If file exceeds limit, `media_data` is set to `None`

---

### 3.5 `backends/matrix_target.py` — Server B Backend

**Responsibility:** Connect to Server B, monitor the aggregation room, parse reply commands and control commands, detect reply-to messages.

#### Initialization

1. Call `_init_client()` for authentication
2. Register callbacks: `RoomMessage`, `MegolmEvent`, common callbacks
3. Perform initial sync with `full_state=True` to load room state
4. Restore sync position
5. Query device keys for target room members
6. Start sync loop with `_after_sync` hook for undecrypted event detection

#### Event Processing

```
Incoming message in target_room
    │
    ├─ From own device? ──► SKIP (loop prevention)
    ├─ Not in target_room? ──► SKIP
    ├─ event_id processed? ──► SKIP (dedup)
    │
    ├─ Has m.in_reply_to? ──► BridgeMessage(direction=REPLY, reply_to_event_id=...)
    │
    ├─ Matches control command? ──► BridgeMessage(direction=CONTROL, text="login"|"logout"|...)
    │   Commands: !login, !logout, !pause, !resume, !status
    │
    ├─ Starts with command_prefix? ──► Parse "!send #room message"
    │     ├─ Valid ──► BridgeMessage(direction=REPLY, target_room_id=..., text=...)
    │     └─ Invalid ──► Send usage help
    │
    └─ Media event ──► download media ──► BridgeMessage(direction=REPLY, msgtype=IMAGE/...)
```

#### Reply-to Support

When a user replies to a forwarded message in the target room, the backend:
1. Extracts `m.in_reply_to.event_id` from the event content
2. Strips the Matrix reply fallback quote from the body
3. Emits a `REPLY` message with `reply_to_event_id` set
4. `BridgeCore` resolves the reply to the correct source room

#### Control Command Routing

Control commands are derived from the first character of `command_prefix`:
- Default prefix `!send` → control prefix `!`
- Commands: `!login`, `!logout`, `!pause`, `!resume`, `!status`

Detected as exact string match on the stripped message body.

#### Undecrypted Event Detection

After each sync, `_check_undecrypted_events()` scans for `MegolmEvent` entries in the target room timeline that could not be decrypted. For each, it sends a notice: `"⛔ Unable to decrypt message from {sender}"` and queues the event for retry.

#### Extra Methods

| Method | Description |
|--------|-------------|
| `get_event_body(room_id, event_id)` | Fetch an event's body text via `room_get_event` API |
| `send_reaction(room_id, event_id, key)` | Send an `m.reaction` annotation (default key: ✓) |
| `send_message(room_id, text, msgtype)` | Override default msgtype to `m.notice` |

---

### 3.6 `bridge/core.py` — Message Router

**Responsibility:** Connect source and target backends, route messages, handle control commands, manage backup mode.

#### Initialization

```python
class BridgeCore:
    _source: BaseBackend
    _target: Optional[BaseBackend]     # None in backup mode
    _backup_mode: bool                 # True when target is None
    _store: Optional[MessageStore]     # SQLite persistence layer
    _forwarding_enabled: bool          # Toggled by !login/!logout
    _forwarding_paused: bool           # Toggled by !pause/!resume
    _admin_users: set[str]             # Authorized command users
    _source_to_target_map: dict        # source_event_id → target_event_id
    _room_id_map: dict                 # target_event_id → source_room_id
```

#### Startup Behavior

```
Bridge mode:
    1. Start target backend
    2. Check saved state: was forwarding_enabled True?
       ├─ Yes ──► Start source backend, forwarding active
       └─ No  ──► Skip source, wait for !login command
    3. Check forwarding_paused from state

Backup mode:
    1. Start source backend immediately
    2. Set forwarding_enabled = True
    3. All messages saved to store, none forwarded
```

#### A → B Forwarding (`_on_source_message`)

```
BridgeMessage from source
    │
    ├─ direction == REDACT ──► _on_source_redact()
    ├─ direction == EDIT   ──► _on_source_edit()
    ├─ direction != FORWARD ──► SKIP
    │
    ├─ Save to MessageStore (if enabled)
    ├─ from_self == True ──► SKIP (don't forward own messages)
    ├─ backup_mode ──► SKIP (no forwarding)
    ├─ !forwarding_enabled || forwarding_paused ──► SKIP
    │
    ├─ msgtype == CALL_NOTIFICATION ──► _forward_call_notification()
    ├─ msgtype in [IMAGE, VIDEO, AUDIO, FILE] AND media_data ──► _forward_media()
    └─ msgtype in [TEXT, NOTICE, EMOTE] ──► _forward_text()
```

#### Edit Forwarding (`_on_source_edit`)

1. Update message text in MessageStore
2. Look up the target event ID via `_source_to_target_map`
3. If found, call `target.edit_message()` to edit the forwarded message
4. Edits during pause or from self are not forwarded

#### Redaction Forwarding (`_on_source_redact`)

1. Delete the message from MessageStore
2. Look up the target event ID via `_source_to_target_map`
3. If found, call `target.redact_event()` to redact on Server B
4. Remove the mapping from state

#### B → A Reply (`_on_target_message`)

```
BridgeMessage from target
    │
    ├─ backup_mode ──► SKIP
    ├─ direction == CONTROL ──► _handle_control()
    ├─ direction != REPLY ──► SKIP
    │
    ├─ Has reply_to_event_id? ──► Resolve source room
    │     ├─ Check _room_id_map (in-memory)
    │     └─ Fallback: fetch event body, parse [room_name] prefix
    │
    ├─ Has target_room_id? ──► Resolve room alias → room ID
    │
    └─ Forward to source:
          ├─ Media? ──► source.send_media()
          └─ Text? ──► source.send_message()
          Then: target.send_reaction(✓) on the reply event
```

#### Control Command Handling (`_handle_control`)

```
Control message from target room
    │
    ├─ backup_mode ──► SKIP
    ├─ Wrong room? ──► SKIP
    ├─ admin_users set AND sender not in admin_users? ──► SKIP
    │
    ├─ "login"  ──► Start source, set forwarding_enabled=True
    ├─ "logout" ──► Stop source, set forwarding_enabled=False, clear all mappings
    ├─ "pause"  ──► Set forwarding_paused=True (source stays connected)
    ├─ "resume" ──► Set forwarding_paused=False
    ├─ "status" ──► Send status notice (source connected?, forwarding state)
    │
    └─ Persist state after each command
```

#### Event Mapping Persistence

Two bidirectional maps are maintained for edit/redaction/reply resolution:

| Map | Purpose | Max Size |
|-----|---------|----------|
| `source_target_map` | source_event_id → target_event_id | 5,000 |
| `event_room_map` | target_event_id → source_room_id | 5,000 |

Both are persisted in `state.json` and evicted on FIFO basis.

---

### 3.7 `bridge/state.py` — State Persistence

**Storage format:** JSON file (`state.json`)

```json
{
  "sync_tokens": {
    "source": "s3_12345_abc",
    "target": "s3_67890_def"
  },
  "processed_events": ["$event1", "$event2"],
  "forwarding_enabled": true,
  "forwarding_paused": false,
  "event_room_map": {"$target_event1": "!room:a.com"},
  "source_target_map": {"$source_event1": "$target_event1"},
  "failed_decryptions": {
    "session_id": [{"room_id": "...", "event_id": "..."}]
  }
}
```

**Operations:**

| Method | Description |
|--------|-------------|
| `load()` | Read state.json on startup |
| `save_sync_token(backend, token)` | Store sync batch token per backend |
| `load_sync_token(backend)` | Restore sync position after restart |
| `is_processed(event_id)` | Check if event was already handled |
| `mark_processed(event_id)` | Record event to prevent duplicate processing |
| `save_event_room(event_id, room_id)` | Map target event → source room |
| `get_event_room(event_id)` | Look up source room for a target event |
| `save_source_target(source_id, target_id)` | Map source event → target event |
| `get_target_event_id(source_id)` | Look up target event for a source event |
| `pop_source_target(source_id)` | Remove and return a mapping |
| `clear_mappings()` | Clear all event maps (on logout) |
| `get_forwarding_enabled()` | Get forwarding state |
| `set_forwarding_enabled(bool)` | Set forwarding state |
| `get_forwarding_paused()` | Get pause state |
| `set_forwarding_paused(bool)` | Set pause state |
| `save_failed_decryption(session_id, room_id, event_id)` | Persist failed decryption for cross-restart retry |
| `pop_failed_decryptions(session_id)` | Retrieve and remove persisted failures |
| `flush()` | Write state to disk (if dirty) |

**Eviction policies:**
- `processed_events`: 10,000 entries (FIFO)
- `event_room_map`: 5,000 entries (FIFO)
- `source_target_map`: 5,000 entries (FIFO)
- `failed_decryptions`: 500 total entries across all sessions

**Flush timing:** State is flushed to disk every 60 seconds (periodic task) and on graceful shutdown. File is written atomically (temp file + `os.replace`) with owner-only permissions.

---

### 3.8 `bridge/message_store.py` — SQLite Message Persistence

Peewee ORM-based message store with full-text search, alias management, and media file storage.

#### Database Schema

**`messages` table:**

| Column | Type | Description |
|--------|------|-------------|
| `id` | AutoField (PK) | Row ID, also FTS rowid |
| `timestamp` | DateTime | UTC timestamp |
| `direction` | CharField | "forward", "reply", "control", "edit", "redact" |
| `source_room_id` | CharField | |
| `source_room_name` | CharField | |
| `sender` | CharField | |
| `sender_displayname` | CharField | |
| `text` | TextField | Message body |
| `msgtype` | CharField | "m.text", "m.image", etc. |
| `event_id` | CharField (UNIQUE) | Matrix event ID |
| `target_room_id` | CharField | |
| `media_url` | CharField | Original mxc:// URI |
| `media_filename` | CharField | |
| `media_mimetype` | CharField | |
| `media_size` | IntegerField | |
| `call_type` | CharField | "voice" / "video" |
| `call_action` | CharField | "started" / "answered" / "ended" |
| `call_duration` | IntegerField | Seconds |
| `from_self` | BooleanField | True if sender is the bridge bot |
| `media_local_path` | CharField | Relative path within media_dir |
| `edit_of_event_id` | CharField (Indexed) | References the original event |

**`bridge_config` table:** Key-value store for internal state (e.g., `web_secret`, `migrated_aliases_v1`).

**`user_aliases` table:** Maps `sender_id` (PK) → `displayname`.

**`room_aliases` table:** Maps `room_id` (PK) → `room_name`.

**`messages_fts` virtual table:** FTS5 full-text index on `text`, kept in sync via INSERT/DELETE/UPDATE triggers.

#### SQLite Pragmas

- `journal_mode = wal` (Write-Ahead Logging)
- `busy_timeout = 5000` (5 second lock timeout)

#### Key Features

| Feature | Description |
|---------|-------------|
| Schema migration | Automatically adds `from_self`, `media_local_path`, `edit_of_event_id` columns if missing |
| Alias migration | Populates `user_aliases` and `room_aliases` from existing messages on first run |
| Media storage | Saves files to `YYYY-MM/` subdirectories with atomic writes |
| Edit reconciliation | `reconcile_edits()` resolves edit chains, applies latest text, removes edit stubs |
| FTS5 search | Full-text search with automatic trigger-synced index, falls back to LIKE |
| Deduplication | `event_id` UNIQUE constraint silently catches duplicates |
| Alias enrichment | Search results replace IDs with latest known display names |

#### Integration Points

- `bridge/core.py`: Calls `save_message()`, `upsert_user_alias()`, `upsert_room_alias()`, `update_message_text()`, `delete_message()` from async threads
- `scripts/backfill.py`: Bulk imports historical messages
- `bridge/web.py`: Serves stored messages via HTTP API

---

### 3.9 `bridge/web.py` — Web Search Interface

An aiohttp-based HTTP server providing a searchable web interface for the message store.

#### Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | No | Serves the HTML UI (`index.html`) |
| POST | `/api/login` | No | Authenticates, returns HMAC-signed bearer token |
| GET | `/api/stats` | Yes | Total messages, rooms, forward/reply counts, date range |
| GET | `/api/rooms` | Yes | Lists rooms with message count and last message timestamp |
| GET | `/api/rooms/{room_id}/senders` | Yes | Lists senders for a room with display name and count |
| GET | `/api/search?q=&room=&sender=&from=&to=&page=&limit=` | Yes | Full-text search with filtering and pagination |
| GET | `/api/history/{room_id}?page=&limit=` | Yes | Paginated room message history (ascending) |
| GET | `/api/context/{event_id}?before=&after=` | Yes | Message with N surrounding messages for context |
| GET | `/api/media/{event_id}` | Yes | Serves a saved media file from local storage |
| GET | `/static/*` | No | Static files from `bridge/templates/` |

#### Authentication

- **Token-based**: HMAC-SHA256 signed tokens with 7-day expiry
- **Secret**: Auto-generated 32-byte random secret, persisted in `bridge_config` table
- **No-password mode**: If `web.password` is empty, binds to `127.0.0.1` only, auto-issues tokens
- **Password mode**: If password is set, requires login via `/api/login`
- **Token delivery**: `Authorization: Bearer <token>` header or `?token=` query parameter
- **Rate limiting**: 10 login attempts per 60 seconds per client IP

#### Security

- **Reverse proxy support**: `trusted_proxy` flag reads real IP from `X-Forwarded-For`
- **Path traversal protection**: Media serving verifies resolved path stays within `media_dir`
- **HMAC constant-time comparison**: `hmac.compare_digest()` for token and password checks

---

### 3.10 `bridge/crypto.py` — Config Field Encryption

Symmetric encryption for sensitive config values (access tokens, passwords, key passphrases).

| Function | Description |
|----------|-------------|
| `encrypt(plaintext, master_password)` | Encrypt a value, returns `enc:...` prefixed string |
| `decrypt(encrypted_value, master_password)` | Decrypt an `enc:...` value |
| `is_encrypted(value)` | Check if a value starts with `enc:` prefix |
| `decrypt_config(config, master_password)` | Walk source/target sections, decrypt all encrypted fields |

**Encryption details:**
- Algorithm: Fernet (AES-128-CBC with HMAC-SHA256 for authentication)
- Key derivation: PBKDF2-HMAC-SHA256, 600,000 iterations, 16-byte random salt
- Encrypted values prefixed with `enc:` for easy identification
- Supported fields: `access_token`, `password`, `key_import_passphrase`

---

### 3.11 `main.py` — Entry Point

```
1. Load config.yaml
2. Setup logging (stdout or rotating file)
3. Check for encrypted fields (enc: prefix)
   └─ Read MXBIRDGE_MASTER_KEY env var, or prompt for master password, call decrypt_config()
4. Interactive credential setup (if access_token missing)
   ├─ Login with password via _matrix_login()
   ├─ Read MXBIRDGE_MASTER_KEY env var, or prompt for master password (with confirmation)
   ├─ Encrypt token with master password
   ├─ Write back to config.yaml
   └─ Optional: import encryption keys
5. Initialize StateManager, load persisted state
6. Determine mode: bridge (has target) or backup (no target)
7. Initialize MessageStore (if message_store.enabled)
8. Create backends:
   ├─ MatrixSourceBackend (always)
   └─ MatrixTargetBackend (bridge mode only)
9. Create BridgeCore
10. Start WebServer (if web.enabled and message_store active)
11. Register SIGINT/SIGTERM handlers
12. asyncio.run() — start bridge + web server
13. On signal: cancel bridge task → stop backends → stop web → flush state → close DB → exit
```

#### Interactive Credential Setup

When `access_token` is missing from a backend section, `setup_credentials()`:
1. Reads `MXBIRDGE_MASTER_KEY` env var, or prompts for a master password (with confirmation)
2. Logs in with the configured password
3. Encrypts the received access token with the master password
4. Writes the encrypted token back to `config.yaml`
5. Offers optional E2EE key file import

---

### 3.12 CLI Tools

#### `scripts/backfill.py` — Historical Message Import

Connects to the source Matrix server and bulk-imports historical room messages into the MessageStore.

**Features:**
- Paginated `/messages` API traversal (batch size: 250)
- E2EE support via the source bot's crypto store
- Media download and local storage
- Edit reconciliation after import
- Redaction handling (deletes redacted messages)
- Config encryption support

**CLI flags:**
```
--rooms       Comma-separated room IDs/aliases (default: all joined)
--days N      Import last N days (default: 30)
--limit N     Max total messages to import
--no-media    Skip media downloads
--dry-run     Show what would be imported
--log-level   Set log level
```

#### `scripts/repair_media.py` — Media Repair Tool

Scans locally saved media files for corruption (encrypted ciphertext saved without decryption) and re-downloads + re-decrypts them.

**Features:**
- Magic byte detection against 25+ known media format signatures
- Re-download + decrypt via `decrypt_attachment()`
- Atomic file replacement
- Path traversal protection
- `--dry-run` mode

#### `scripts/encrypt_tool.py` — Config Encryption Utility

Interactive CLI for encrypting/decrypting individual config values:

```
python scripts/encrypt_tool.py encrypt    # Encrypt a value
python scripts/encrypt_tool.py decrypt    # Decrypt a value
```

---

## 4. Data Flow Diagrams

### 4.1 Text Message Forwarding (A → B)

```
Server A                    Source Backend            BridgeCore              Target Backend              Server B
  │                              │                        │                        │                        │
  │── m.room.message ───────────►│                        │                        │                        │
  │   (Alice: "Hello")           │                        │                        │                        │
  │                              │── BridgeMessage ──────►│                        │                        │
  │                              │  FORWARD, TEXT         │                        │                        │
  │                              │                        │── save to store ──────►│                        │
  │                              │                        │── format text ────────►│                        │
  │                              │                        │  "[#general] Alice:    │                        │
  │                              │                        │   Hello"               │── m.room.message ─────►│
  │                              │                        │                        │   (m.notice)           │
  │                              │                        │── save event map ─────►│                        │
```

### 4.2 Media Forwarding (A → B)

```
Server A                    Source Backend            BridgeCore              Target Backend              Server B
  │                              │                        │                        │                        │
  │── m.room.message ───────────►│                        │                        │                        │
  │   (m.image, mxc://A/xxx)     │                        │                        │                        │
  │                              │── download(mxc://A/xxx)│                        │                        │
  │◄── binary data ─────────────│                        │                        │                        │
  │                              │── BridgeMessage ──────►│                        │                        │
  │                              │  FORWARD, IMAGE,       │                        │                        │
  │                              │  media_data=<bytes>    │── save to store ──────►│                        │
  │                              │                        │── send_media() ───────►│                        │
  │                              │                        │                        │── upload(bytes) ───────►│
  │                              │                        │                        │◄── mxc://B/yyy ────────│
  │                              │                        │                        │── m.room.message ─────►│
  │                              │                        │                        │   (url: mxc://B/yyy)   │
  │                              │                        │                        │── m.notice (caption) ─►│
```

### 4.3 Edit Forwarding (A → B)

```
Server A                    Source Backend            BridgeCore              Target Backend              Server B
  │                              │                        │                        │                        │
  │── m.room.message ───────────►│                        │                        │                        │
  │   (m.replace, new_content)   │                        │                        │                        │
  │                              │── BridgeMessage ──────►│                        │                        │
  │                              │  EDIT, edit_of=$orig   │                        │                        │
  │                              │                        │── update store ───────►│                        │
  │                              │                        │── lookup target event ►│                        │
  │                              │                        │                        │                        │
  │                              │                        │── edit_message() ─────►│── m.room.message ─────►│
  │                              │                        │  (m.replace)           │   (edited message)     │
```

### 4.4 Redaction Forwarding (A → B)

```
Server A                    Source Backend            BridgeCore              Target Backend              Server B
  │                              │                        │                        │                        │
  │── m.room.redaction ─────────►│                        │                        │                        │
  │                              │── BridgeMessage ──────►│                        │                        │
  │                              │  REDACT                │── delete from store ──►│                        │
  │                              │                        │── lookup target event ►│                        │
  │                              │                        │── redact_event() ─────►│── m.room.redaction ───►│
  │                              │                        │── remove mapping ─────►│                        │
```

### 4.5 Reply Command (B → A)

```
Server B                    Target Backend            BridgeCore              Source Backend             Server A
  │                              │                        │                        │                        │
  │── m.room.message ───────────►│                        │                        │                        │
  │   "!send #general Hi"        │                        │                        │                        │
  │                              │── parse command ──────►│                        │                        │
  │                              │  REPLY, target=#general │                        │                        │
  │                              │                        │── resolve_room_id ────►│                        │
  │                              │                        │  "#general" → "!abc"    │                        │
  │                              │                        │                        │                        │
  │                              │                        │── send_message ────────►│── m.room.message ─────►│
  │                              │                        │  "[Bob from bridge] Hi" │                        │
  │                              │                        │                        │                        │
  │                              │◄── send_reaction(✓) ───│                        │                        │
```

### 4.6 Reply-to (B → A)

```
Server B                    Target Backend            BridgeCore              Source Backend             Server A
  │                              │                        │                        │                        │
  │── m.room.message ───────────►│                        │                        │                        │
  │   (m.in_reply_to: $fwd_ev)   │                        │                        │                        │
  │                              │── REPLY ──────────────►│                        │                        │
  │                              │  reply_to=$fwd_ev      │                        │                        │
  │                              │                        │── lookup room map ────►│                        │
  │                              │                        │  $fwd_ev → room "!abc"  │                        │
  │                              │                        │── send_message ────────►│── m.room.message ─────►│
  │                              │                        │                        │                        │
```

### 4.7 Control Command (!login)

```
User in Target Room          Target Backend            BridgeCore              Source Backend             Server A
  │                              │                        │                        │                        │
  │── "!login" ─────────────────►│                        │                        │                        │
  │                              │── CONTROL ────────────►│                        │                        │
  │                              │  text="login"          │                        │                        │
  │                              │                        │── source.start() ─────►│── connect ────────────►│
  │                              │                        │── set forwarding=True   │                        │
  │                              │                        │── persist state ───────►│                        │
  │                              │                        │                        │                        │
  │◄── "Source connected" ───────│◄── send_notice() ─────│                        │                        │
```

---

## 5. E2EE Details

### 5.1 Key Lifecycle

```
First run:
  1. AsyncClient generates Identity Key (Ed25519 + Curve25519)
  2. Generates One-Time Keys (OTK)
  3. Uploads public keys to server via /keys/upload
  4. Persists all keys to store_path (SQLite)

Subsequent runs:
  1. Loads existing keys from store_path
  2. Uploads new OTKs if needed (should_upload_keys)
  3. Queries other users' keys (should_query_keys)

Key import (optional):
  1. Load key export file (e.g., from Element)
  2. Import Megolm session keys via client.import_keys()
  3. Clear key_import_file and key_import_passphrase from runtime config
```

### 5.2 Decryption Flow

```
Encrypted event (MegolmEvent)
    │
    ├─ OlmMachine retrieves Megolm session key
    │  (received via to-device from room members)
    │
    ├─ Decrypts payload
    │
    └─ Success ──► Re-dispatch as RoomMessage
       │
       └─ Failure ──► Queue for retry
           ├─ Save to _pending_encrypted
           ├─ Request room key
           ├─ Query + claim sender's device keys
           ├─ Persist to StateManager for cross-restart retry
           └─ Retry when key arrives (in-memory or persisted)
```

### 5.3 Critical Requirements for E2EE

| Requirement | Why |
|-------------|-----|
| `device_id` must never change | Changing it creates a new device, losing all session keys |
| `store_path` must persist | Contains Olm/Megolm session keys — deletion is irreversible |
| Bot must be in room before messages are sent | Megolm session keys are distributed at send time |
| Users should verify the bot device | Prevents "unverified device" warnings in clients |
| `key_import_file` can bootstrap decryption | Import keys from another client to decrypt historical messages |

---

## 6. Loop Prevention & Deduplication

### Four-layer protection:

| Layer | Mechanism | Location |
|-------|-----------|----------|
| 1. Sender check | `event.sender == self.config["user_id"]` + device check → skip | Both backends |
| 2. Event dedup | `state.is_processed(event_id)` → skip | Both backends |
| 3. Direction filter | Only process FORWARD/EDIT/REDACT in source, REPLY/CONTROL in target | BridgeCore |
| 4. Self-message filter | `msg.from_self == True` → skip forwarding | BridgeCore |

---

## 7. Extending with New Protocols

### Adding a Telegram backend (example)

**Step 1:** Create `backends/telegram.py`

```python
class TelegramBackend(BaseBackend):
    async def start(self):
        # Connect to Telegram Bot API
        # Poll for updates
        # On message: construct BridgeMessage → self._emit_message()

    async def stop(self):
        pass

    async def send_message(self, room_id, text, msgtype="m.text"):
        pass

    async def send_media(self, room_id, data, mimetype, filename, msgtype="m.file", extra_info=None):
        pass

    async def redact_event(self, room_id, event_id, reason=None):
        pass

    async def edit_message(self, room_id, event_id, new_text, msgtype="m.notice"):
        pass

    async def resolve_room_id(self, room_alias_or_id):
        pass
```

**Step 2:** Update `config.yaml`

**Step 3:** Update `main.py`

No changes needed to `BridgeCore`, `BridgeMessage`, or `MessageStore`.

---

## 8. Configuration Schema

```yaml
logging:
  level: string                    # DEBUG | INFO | WARNING | ERROR
  file: string                     # Log file path (empty = stdout only)
  max_bytes: integer               # Max log file size before rotation (default: 10MB)
  backup_count: integer            # Number of rotated log files to keep (default: 3)

source:                            # Backend connecting to Server A
  type: string                     # "matrix" (future: "telegram", etc.)
  homeserver: string               # Required. e.g. "https://matrix-a.example.com"
  user_id: string                  # Required. Full MXID
  access_token: string             # Access token or password required
  password: string                 # Used only on first run
  device_id: string                # Must be fixed for E2EE
  store_path: string               # E2EE crypto store directory
  handle_encrypted: boolean        # default: true
  media_max_size: integer          # Max media download size in bytes (default: 50MB)
  key_import_file: string          # Path to E2EE key export file (optional)
  key_import_passphrase: string    # Passphrase for the key export file

target:                            # Backend connecting to Server B (omit for backup mode)
  type: string
  homeserver: string
  user_id: string
  access_token: string
  password: string
  device_id: string
  store_path: string
  handle_encrypted: boolean
  target_room: string              # Room ID of the aggregation room
  key_import_file: string
  key_import_passphrase: string

bridge:
  command_prefix: string           # default: "!send"
  message_format: string           # Template with {room_name}, {sender}, {text}
  state_path: string               # default: "state.json"
  admin_users: list[string]        # List of MXIDs authorized to issue control commands (empty = anyone)
  media:
    enabled: boolean               # default: true
  call_notifications:
    enabled: boolean               # default: true
  message_store:
    enabled: boolean               # default: false
    path: string                   # SQLite database path (default: "messages.db")
    media_dir: string              # Local directory for media files (default: "./media")
  web:
    enabled: boolean               # default: false (requires message_store)
    host: string                   # Bind host (default: "0.0.0.0")
    port: integer                  # Bind port (default: 8080)
    password: string               # Required for remote access; empty = localhost only
    trusted_proxy: boolean         # Trust X-Forwarded-For header (default: false)
```

---

## 9. Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `matrix-nio[e2e]` | >= 0.24.0 | Async Matrix client with E2EE support |
| `PyYAML` | >= 6.0 | Configuration file parsing |
| `cryptography` | >= 42.0 | Fernet encryption for config field encryption |
| `aiohttp` | >= 3.9.0 | HTTP server for web search interface |
| `peewee` | >= 3.17.0 | SQLite ORM for message persistence |

### matrix-nio sub-dependencies for E2EE

| Package | Purpose |
|---------|---------|
| `python-olm` | Olm/Megolm cryptographic operations |
| `pycryptodome` | AES/HMAC for message encryption |
| `atomicwrites` | Atomic file writes for key storage |
| `cachetools` | Key caching |
| `unpaddedbase64` | Matrix-specific base64 encoding |

### Indirect dependencies (via matrix-nio)

| Package | Purpose |
|---------|---------|
| `aiofiles` | Async file I/O for state persistence |
