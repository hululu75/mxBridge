# Changelog

All notable changes to the Matrix Bridge project are documented in this file.

## [0.4.0] - 2026-04-21

### Added

- **Backup mode** — run without a `target` section to save all messages and media to local SQLite storage only
- **Control commands** (`!login`, `!logout`, `!pause`, `!resume`, `!status`) for runtime bridge management
- **Admin authorization** (`bridge.admin_users`) to restrict control commands to specific MXIDs
- **Message edit forwarding** — edits on Server A are applied to forwarded messages on Server B
- **Message redaction forwarding** — redactions on Server A are applied to forwarded messages on Server B
- **Reply-to support** — users on Server B can use Matrix reply threading to reply to the correct source room
- **Web search interface** (`bridge/web.py`) — aiohttp-based HTTP API with full-text search, room browsing, media serving
  - HMAC-SHA256 token authentication with 7-day expiry
  - Rate limiting (10 attempts / 60 seconds per IP)
  - Reverse proxy support (`X-Forwarded-For`)
- **Message persistence** (`bridge/message_store.py`) — SQLite-backed store with FTS5 full-text search
  - Schema auto-migration (from_self, media_local_path, edit_of_event_id)
  - User/room alias enrichment
  - Edit reconciliation
  - Media file storage with atomic writes
- **Shared Matrix base backend** (`backends/matrix_base.py`) — extracted common logic from source/target
  - Pending encrypted event queue with automatic key re-request
  - SAS key verification auto-acceptance
  - E2EE key import (`key_import_file` / `key_import_passphrase`)
  - Server-assigned `device_id` persistence to config file
- **Backfill tool** (`backfill.py`) — import historical messages with E2EE support, edit reconciliation, and media download
- **Media repair tool** (`repair_media.py`) — detect and fix corrupted encrypted media files
- **Log file rotation** (`logging.file`, `logging.max_bytes`, `logging.backup_count`)
- **Interactive credential setup** — first-run login with automatic token encryption and config write-back
- **Message reaction** — ✓ reaction added to command messages after successful reply delivery
- **Undecrypted event detection** — sends notice in target room for messages that cannot be decrypted
- **`aiohttp>=3.9.0`** and **`peewee>=3.17.0`** added as direct dependencies

### Changed

- **Documentation fully rewritten** — ARCHITECTURE.md and USERGUIDE.md updated to cover all features
- **Chinese translations added** — ARCHITECTURE_CN.md and USERGUIDE_CN.md created
- `BaseBackend` now requires `redact_event()` and `edit_message()` abstract methods
- `MessageDirection` enum extended with `CONTROL`, `REDACT`, `EDIT` values
- `BridgeMessage` extended with `from_self`, `edit_of_event_id`, `reply_to_event_id`, `redacted_event_id` fields
- `StateManager` extended with event room mapping, source-target mapping, forwarding state, and failed decryption persistence
- `peewee` promoted from indirect to direct dependency

## [0.3.0] - 2026-04-15

### Added

- **State persistence v2** — event room maps, source→target event maps, forwarding state, failed decryption tracking
- **Pending encrypted event queue** — automatic retry when Megolm session keys arrive, persisted across restarts
- **SAS key verification auto-acceptance** — bridge automatically accepts and completes interactive verification
- **E2EE key import** — `key_import_file` and `key_import_passphrase` config fields for importing Megolm session keys
- **Shared MatrixBackend base class** (`backends/matrix_base.py`) — extracted common auth, sync, media, and key logic
- **Server-assigned device_id persistence** — writes new device_id back to config file automatically

### Changed

- `StateManager` now persists `forwarding_enabled`, `forwarding_paused`, `event_room_map`, `source_target_map`, `failed_decryptions`
- Source backend registers `RedactionEvent` callback for redaction forwarding
- Both backends use shared `MatrixBackend._register_common_callbacks()` for verification and key events

## [0.2.0] - 2026-03-29

### Added

- **Config field encryption** (`bridge/crypto.py`)
  - AES encryption via Fernet (PBKDF2-SHA256 key derivation) for sensitive config values
  - `encrypt()`, `decrypt()`, `is_encrypted()`, `decrypt_config()` API
  - Encrypted values prefixed with `enc:` in config.yaml for easy identification
  - Supports `source.access_token`, `source.password`, `target.access_token`, `target.password`
- **Encryption CLI tool** (`encrypt_tool.py`)
  - `python encrypt_tool.py encrypt` — interactively encrypt a value
  - `python encrypt_tool.py decrypt` — interactively decrypt a value
  - Password confirmation on encrypt, error handling on wrong password
- **Interactive master password prompt** in `main.py`
  - Bridge prompts for master password at startup when `enc:` values are detected
  - Auto-skips prompt when no encrypted fields exist
  - Clean error message on wrong password
- `cryptography>=42.0` dependency added to `requirements.txt`

## [0.1.1] - 2026-03-29

### Fixed

- **Critical: `download()` API call** in `backends/matrix_source.py`
  - Changed `download(mxc_uri=...)` to `download(mxc=...)` to match matrix-nio 0.25.x API
  - Added proper `DownloadError` type checking instead of generic `hasattr`
- **Unused imports removed**
  - `from nio.crypto import TrustState` removed from `matrix_source.py`
  - `import re` removed from `matrix_target.py`
  - `import time` removed from `bridge/state.py`
- **Confusing ternary expression** in `_handle_call_hangup` replaced with clear if/else
- **Redundant mimetype fallback** simplified in `_handle_media` (removed double `getattr` chain)
- **Unused `_get_room_name(room)` call** removed from `_on_encrypted_event`
- **Inline `from io import BytesIO`** moved to top-level imports in both `matrix_source.py` and `matrix_target.py`
- **Logging config ordering** in `main.py` — `setup_logging("INFO")` is now called before config loading, so error messages are properly formatted

## [0.1.0] - 2026-03-29

### Added

- **Project structure**
  - `main.py` — entry point with signal handling and graceful shutdown
  - `config.example.yaml` — configuration template
  - `requirements.txt` — Python dependencies
- **Core bridge modules**
  - `bridge/models.py` — `BridgeMessage` dataclass with `MessageDirection`, `MessageType`, `CallAction` enums
  - `bridge/core.py` — `BridgeCore` message router (A→B forwarding, B→A reply routing)
  - `bridge/state.py` — `StateManager` with sync token persistence and event deduplication (JSON-based)
- **Backend adapters**
  - `backends/base.py` — `BaseBackend` abstract base class for protocol extensibility
  - `backends/matrix_source.py` — Server A backend (monitors all rooms, emits FORWARD messages)
  - `backends/matrix_target.py` — Server B backend (aggregation room + `!send` command parser)
- **Message forwarding features**
  - Text, notice, and emote message forwarding (A→B)
  - Media forwarding (image, video, audio, file) with download→upload pipeline
  - Call notification forwarding (started, answered, ended) with voice/video detection from SDP
  - Reverse reply via `!send #room_alias message` command (B→A)
- **E2EE support**
  - matrix-nio based encryption/decryption with persistent crypto store (SQLite)
  - Automatic key upload and query on first run
  - Fixed `device_id` for key consistency across restarts
- **Loop prevention**
  - Sender check (skip own messages)
  - Event deduplication via `processed_events` set
  - Direction filtering in `BridgeCore`
- **Documentation**
  - `USERGUIDE.md` — installation, configuration, usage, troubleshooting
  - `ARCHITECTURE.md` — technical architecture, data flow diagrams, module specs, extension guide
