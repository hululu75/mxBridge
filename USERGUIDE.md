# Matrix Bridge — User Guide

## Overview

Matrix Bridge is a self-hosted service that forwards messages between two Matrix servers. It monitors all rooms on **Server A** and forwards messages to a single aggregation room on **Server B**, with support for reverse replies via commands and Matrix reply-to threading.

### Operating Modes

| Mode | Description |
|------|-------------|
| **Bridge mode** (default) | Forward messages from A → B, support reverse replies and control commands |
| **Backup mode** | Save all messages + media to local SQLite storage without forwarding |

### Features

| Feature | Description |
|---------|-------------|
| Text forwarding | All text, notice, and emote messages from A → B |
| Media forwarding | Images, videos, audio, and files (download → re-upload) |
| Edit forwarding | Message edits on A are applied to the forwarded message on B |
| Redaction forwarding | Redactions on A are applied to the forwarded message on B |
| Call notifications | Call started / answered / ended events forwarded as notices |
| Reverse replies | Users on B can send messages back to A via `!send` command |
| Reply-to support | Users on B can reply directly to forwarded messages (Matrix threading) |
| Control commands | `!login`, `!logout`, `!pause`, `!resume`, `!status` for runtime control |
| E2EE support | Decrypts encrypted rooms on Server A (via matrix-nio) |
| Config encryption | Encrypt sensitive config values (access tokens, passwords) with a master password |
| Message store | SQLite-based message persistence with full-text search |
| Web interface | Searchable web UI for browsing stored messages |
| State persistence | Survives restarts without re-processing old messages |
| Log file rotation | Optional rotating file-based logging |

## Prerequisites

- Python 3.11+
- Two Matrix accounts (one on each server) that the bridge will use
- The bridge accounts must already be **invited to and joined** the relevant rooms:
  - Server A account: joined to all rooms you want to forward
  - Server B account: joined to the aggregation room

## Installation

```bash
cd /home/rocky/matrix
pip install -r requirements.txt
```

## Configuration

### 1. Create your config file

```bash
cp config.example.yaml config.yaml
```

### 2. Obtain access tokens

For each server, obtain an access token for the bridge account:

```bash
curl -X POST "https://YOUR_SERVER/_matrix/client/v3/login" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "m.login.password",
    "identifier": {
      "type": "m.id.user",
      "user": "bridge_bot"
    },
    "password": "your_password"
  }'
```

Copy the `access_token` and `device_id` from the response.

### 3. Edit `config.yaml`

#### Bridge mode example

```yaml
logging:
  level: INFO
  file: ""                     # Empty = stdout, or set a file path for log rotation

source:
  homeserver: "https://matrix-a.example.com"
  user_id: "@bridge-bot:a.example.com"
  access_token: "syt_xxxxx..."          # from step 2
  device_id: ""                           # leave empty to auto-assign
  store_path: "./store/source"
  handle_encrypted: true
  media_max_size: 52428800               # 50 MB

target:
  homeserver: "https://matrix-b.example.com"
  user_id: "@bridge-bot:b.example.com"
  access_token: "syt_yyyyy..."
  device_id: ""                           # leave empty to auto-assign
  store_path: "./store/target"
  handle_encrypted: true
  target_room: "!your-aggregation-room:b.example.com"

bridge:
  command_prefix: "!send"
  message_format: "[{room_name}] {sender}: {text}"
  state_path: "state.json"
  admin_users: []                        # Empty = any user can issue commands
  media:
    enabled: true
  call_notifications:
    enabled: true
  message_store:
    enabled: true
    path: "messages.db"
    media_dir: "./media"
  web:
    enabled: false
    host: "0.0.0.0"
    port: 8080
    password: ""                          # Empty = localhost only
    trusted_proxy: false
```

#### Backup mode example

```yaml
logging:
  level: INFO

source:
  homeserver: "https://matrix.example.com"
  user_id: "@backup-bot:example.com"
  access_token: "syt_xxxxx..."
  device_id: ""                           # leave empty to auto-assign
  store_path: "./store/source"
  handle_encrypted: true

# No "target" section — this activates backup mode

bridge:
  message_store:
    enabled: true
    path: "messages.db"
    media_dir: "./media"
  web:
    enabled: true
    port: 8080
    password: "your-web-password"
```

### 4. Encrypt sensitive config values (optional)

Use the encryption tool to encrypt your access tokens and passwords:

```bash
python3 encrypt_tool.py encrypt
```

This will prompt for a value and a master password, then output an `enc:...` string. Replace the plaintext value in `config.yaml`:

```yaml
source:
  access_token: "enc:AAAA..."      # encrypted value
  password: ""                      # no longer needed
```

At startup, the bridge will prompt for the master password to decrypt these values. You can also set the `MXBIRDGE_MASTER_KEY` environment variable to skip the interactive prompt:

```bash
export MXBIRDGE_MASTER_KEY="your-master-password"
python3 main.py
```

### Configuration reference

| Field | Required | Description |
|-------|----------|-------------|
| `logging.level` | No | Log level: DEBUG, INFO, WARNING, ERROR (default: `INFO`) |
| `logging.file` | No | Log file path. Empty = stdout only (default: `""`) |
| `logging.max_bytes` | No | Max log file size before rotation (default: 10MB) |
| `logging.backup_count` | No | Number of rotated log files to keep (default: 3) |
| `source.homeserver` | Yes | Base URL of Server A |
| `source.user_id` | Yes | Full user ID of the bridge account on A |
| `source.access_token` | Yes* | Access token (or use `password` for first login) |
| `source.password` | No | Used on first run to obtain access token |
| `source.device_id` | No | Leave empty to let server auto-assign (saved back to config). Fixed ID recommended for E2EE key consistency |
| `source.store_path` | Yes | Directory for E2EE crypto store (must persist) |
| `source.handle_encrypted` | No | Decrypt encrypted rooms (default: `true`) |
| `source.media_max_size` | No | Max media file size in bytes (default: 50 MB) |
| `source.key_import_file` | No | Path to E2EE key export file (e.g., from Element) |
| `source.key_import_passphrase` | No | Passphrase for the key export file |
| `target.homeserver` | Yes** | Base URL of Server B |
| `target.user_id` | Yes** | Full user ID of the bridge account on B |
| `target.access_token` | Yes** | Access token for Server B |
| `target.target_room` | Yes** | Room ID on Server B where all messages aggregate |
| `bridge.command_prefix` | No | Prefix for reverse-reply commands (default: `!send`) |
| `bridge.message_format` | No | Format template for A→B messages |
| `bridge.admin_users` | No | List of MXIDs authorized for control commands (empty = anyone) |
| `bridge.media.enabled` | No | Forward media files (default: `true`) |
| `bridge.call_notifications.enabled` | No | Forward call notifications (default: `true`) |
| `bridge.message_store.enabled` | No | Enable SQLite message persistence (default: `false`) |
| `bridge.message_store.path` | No | SQLite database file path (default: `messages.db`) |
| `bridge.message_store.media_dir` | No | Local directory to save media files (default: `./media`) |
| `bridge.web.enabled` | No | Enable web search interface (default: `false`) |
| `bridge.web.host` | No | Web server bind host (default: `0.0.0.0`) |
| `bridge.web.port` | No | Web server bind port (default: `8080`) |
| `bridge.web.password` | No | Password for web access. Empty = localhost only |
| `bridge.web.trusted_proxy` | No | Trust X-Forwarded-For header (default: `false`) |

> *You must provide either `access_token` or `password`.
>
> **Required for bridge mode. Omit the entire `target` section for backup mode.

## Running

### Foreground (for testing)

```bash
python3 main.py
```

### With a custom config path

```bash
python3 main.py /path/to/config.yaml
```

### Background (production)

```bash
nohup python3 main.py > bridge.log 2>&1 &
```

### With systemd

Create `/etc/systemd/system/matrix-bridge.service`:

```ini
[Unit]
Description=Matrix Bridge
After=network.target

[Service]
Type=simple
User=rocky
WorkingDirectory=/home/rocky/matrix
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable matrix-bridge
sudo systemctl start matrix-bridge
sudo systemctl status matrix-bridge
```

### First-run interactive setup

If `access_token` is not provided in config but `password` is, the bridge will:
1. Prompt for a master password (for config encryption)
2. Log in to the Matrix server with the provided password
3. Encrypt the received access token and write it back to `config.yaml`
4. Offer to import an E2EE key file

On subsequent starts, you only need to enter the master password (if config values are encrypted), or set the `MXBIRDGE_MASTER_KEY` environment variable to skip the prompt.

## Usage

### A → B (automatic)

All messages from Server A rooms appear in the aggregation room on Server B:

```
[#general] Alice: Hello everyone
[#dev] Bob: The build is passing
📞 Alice started a voice call in [#general]
📞 voice call ended in [#general]
```

Messages edited on A are automatically edited on B. Messages redacted on A are automatically redacted on B.

### B → A (reply command)

Users on Server B send a command in the aggregation room:

```
!send #general Hi from Server B!
!send !abc123:server-a.com Direct room ID also works
```

The message will be sent to the specified room on Server A as the bridge bot, and a ✓ reaction will be added to the command message.

### B → A (reply-to)

Users on Server B can reply directly to any forwarded message in the aggregation room using their Matrix client's reply feature. The reply will be routed to the correct source room automatically.

### Control commands

The following commands are available in the aggregation room on Server B:

| Command | Description |
|---------|-------------|
| `!login` | Connect to Server A and resume forwarding |
| `!logout` | Disconnect from Server A and pause forwarding |
| `!pause` | Pause forwarding (source stays connected, messages still saved) |
| `!resume` | Resume forwarding after pause |
| `!status` | Show current connection and forwarding status |

**Startup behavior:** The bridge starts with only Server B connected. If the source was previously logged out, use `!login` to connect. If it was active, it auto-reconnects.

**Authorization:** If `bridge.admin_users` is set, only listed MXIDs can issue commands. If empty, any user in the target room can issue commands.

### Command syntax

```
!send <room_alias_or_id> <message text>
```

| Argument | Description |
|----------|-------------|
| `room_alias_or_id` | A room alias like `#general:a.example.com` or a room ID like `!abc123:a.example.com` |
| `message text` | The rest of the line is the message content |

## Web Interface

When `bridge.web.enabled` is `true`, a web UI is available for searching and browsing stored messages.

### Access

- If `web.password` is set: visit `http://your-server:8080` and log in with the password
- If `web.password` is empty: only accessible from `http://127.0.0.1:8080` (auto-authenticated)

### Features

- Full-text search across all stored messages
- Filter by room, sender, and date range
- Browse room message history
- View message context (surrounding messages)
- View and download media files
- Statistics dashboard (total messages, rooms, date range)
- **Backfill history** — import historical messages from the source server via the web UI

### Backfill from the Web UI

Click the **Backfill** button in the header to import historical messages:

1. **Days of history**: number of days to fetch (0 = all available history)
2. **Download media files**: whether to download and save media attachments
3. **Clear database & media before backfill**: optionally wipe all existing data and re-download everything

Progress is shown in real-time with room-by-room status updates. Only one backfill can run at a time. Already-existing messages are automatically skipped (no duplicates).

### Reverse proxy

For remote access with TLS, place the web interface behind a reverse proxy:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
}
```

Set `trusted_proxy: true` in the web config to read the real client IP from `X-Forwarded-For`.

## CLI Tools

### Backfill — import historical messages

```bash
# Import last 30 days from all rooms
python3 backfill.py

# Import specific rooms, last 7 days
python3 backfill.py --rooms "#general:a.com,!abc:a.com" --days 7

# Dry run (show what would be imported)
python3 backfill.py --dry-run

# Skip media downloads
python3 backfill.py --no-media

# Limit to 1000 messages
python3 backfill.py --limit 1000
```

### Repair media — fix corrupted files

```bash
# Check and repair corrupted media files
python3 repair_media.py

# Dry run (show what would be repaired)
python3 repair_media.py --dry-run
```

### Encrypt tool — encrypt/decrypt config values

```bash
python3 encrypt_tool.py encrypt    # Encrypt a value
python3 encrypt_tool.py decrypt    # Decrypt a value
```

## Key import

To decrypt historical messages from encrypted rooms, you can import Megolm session keys exported from another client (e.g., Element):

```yaml
source:
  key_import_file: "/path/to/keys.txt"
  key_import_passphrase: "the-passphrase-you-used"
```

Keys are imported on startup. After successful import, you can remove these fields from the config. The import also works during interactive first-run setup.

## Logging

By default, logs are written to stdout. To enable file-based logging with rotation:

```yaml
logging:
  level: INFO
  file: "/var/log/matrix-bridge/bridge.log"
  max_bytes: 10485760      # 10 MB
  backup_count: 3           # Keep 3 rotated files
```

## Troubleshooting

### Messages not appearing on Server B

- Check that the bridge account on A has joined the source rooms
- Check that `target_room` is correct and the bridge account on B has joined it
- Check if forwarding is paused — send `!status` in the target room
- Check logs for sync errors

### E2EE messages not decrypting

- Ensure `handle_encrypted: true`
- Ensure `device_id` has not changed since first run
- Ensure `store_path` directory exists and is writable
- The bridge account must have been in the room when the message was sent
- Try importing keys via `key_import_file` / `key_import_passphrase`
- If messages were sent before the bot joined, keys may need to be re-shared

### "Unable to decrypt" notices in target room

- This means a Megolm session key is missing
- The bridge automatically queues these events and retries when keys arrive
- Failed decryptions persist across restarts
- Check if the sender's device is verified and keys have been shared

### Duplicate messages after restart

- The `state.json` file stores the sync position. If deleted, the bridge will re-process old messages.
- Ensure `state.json` is writable and persists across restarts.

### Media files not forwarding

- Check `media_max_size` — large files are silently skipped
- Check `bridge.media.enabled: true`
- Check disk space and network connectivity between both servers

### Web interface not accessible

- If `web.password` is empty, the interface only binds to `127.0.0.1`
- Set a password and it will bind to the configured host
- For remote access, use a reverse proxy with TLS

### Config decryption fails at startup

- Ensure you are entering the correct master password
- If the password is lost, you will need to re-encrypt your credentials:
  1. Obtain new access tokens
  2. Use `encrypt_tool.py encrypt` with a new master password
  3. Update `config.yaml` with the new encrypted values

## Important notes

- **Do not delete the `store/` directories** — they contain E2EE keys. Deleting them means the bridge loses all decryption ability and must be re-trusted by other users.
- **Do not change `device_id`** after it has been assigned — changing it creates a new device, requiring re-verification. On first run, leave it empty in config and the server will assign one automatically.
- **The bridge account on A will appear as an unverified device** to other users. They can verify it in their client (Element: Settings → Security → Verify device) to suppress warnings.
- **Messages during `!pause` are saved but not forwarded.** When you `!resume`, only new messages will be forwarded — paused messages were already saved to the store.
- **`!logout` clears all event mappings.** After logging back in with `!login`, previously forwarded messages cannot be edited/retracted retroactively.
- **The web interface has no TLS support.** For remote access, place it behind a TLS-terminating reverse proxy (e.g., Nginx, Caddy). Never expose it directly to the internet.
