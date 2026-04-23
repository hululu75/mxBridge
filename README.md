# Matrix Bridge

A self-hosted service that forwards messages between two Matrix homeservers. It monitors rooms on **Server A** and forwards all messages to a single aggregation room on **Server B**, with support for reverse replies, E2EE, and a searchable web interface.

## Features

- **Text & media forwarding** — text, images, video, audio, files, edits, and redactions
- **Call notifications** — forwards call started / answered / ended events
- **Reverse replies** — users on B can send messages back to A via `!send` command or Matrix reply threading
- **E2EE support** — decrypts encrypted rooms via matrix-nio, with automatic SAS verification and key import
- **Config encryption** — encrypt sensitive values (access tokens, passwords) with a master password
- **Backup mode** — save all messages + media to local SQLite without forwarding
- **Web interface** — searchable web UI with full-text search, room browsing, and media viewing
- **Backfill** — import historical messages via CLI or web UI
- **Runtime control** — `!login`, `!logout`, `!pause`, `!resume`, `!status` commands
- **Docker support** — preconfigured Dockerfile and docker-compose

## Quick Start

### Docker (recommended)

```bash
cp config.example.yaml config/config.yaml
# Edit config/config.yaml with your settings

# Set master key for non-interactive startup (if using encrypted config)
export MASTER_KEY="your-master-password"

docker compose up -d
```

### Manual

**Prerequisites:** Python 3.11+, two Matrix accounts (one per server)

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
# Edit config.yaml with your homeserver URLs, user IDs, and tokens
python3 main.py
```

## Configuration

Copy `config.example.yaml` to `config.yaml` and fill in your settings.

### Bridge mode (A → B forwarding)

```yaml
source:
  homeserver: "https://matrix-a.example.com"
  user_id: "@bridge-bot:a.example.com"
  access_token: "syt_xxxxx..."
  device_id: ""
  store_path: "./store/source"
  handle_encrypted: true
  media_max_size: 52428800

target:
  homeserver: "https://matrix-b.example.com"
  user_id: "@bridge-bot:b.example.com"
  access_token: "syt_yyyyy..."
  device_id: ""
  store_path: "./store/target"
  target_room: "!your-aggregation-room:b.example.com"

bridge:
  command_prefix: "!send"
  message_format: "[{room_name}] {sender}: {text}"
  media:
    enabled: true                # Forward media files (default: true)
  call_notifications:
    enabled: true                # Forward call notifications (default: true)
  message_store:
    enabled: true
    path: "messages.db"
    media_dir: "./media"
  web:
    enabled: false
    host: "0.0.0.0"
    port: 8080
    password: ""
```

### Backup mode (local archive only)

Remove the entire `target` section and enable `message_store`:

```yaml
source:
  homeserver: "https://matrix.example.com"
  user_id: "@backup-bot:example.com"
  access_token: "syt_xxxxx..."
  device_id: ""
  store_path: "./store/source"
  handle_encrypted: true

# No "target" section

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

## Running

| Method | Command |
|--------|---------|
| Foreground | `python3 main.py` |
| Custom config | `python3 main.py /path/to/config.yaml` |
| Background | `nohup python3 main.py > bridge.log 2>&1 &` |
| Docker | `docker compose up -d` |
| systemd | See [USERGUIDE.md](USERGUIDE.md#with-systemd) |

### Environment variables

| Variable | Description |
|----------|-------------|
| `MXBIRDGE_MASTER_KEY` | Master password for config decryption (skips interactive prompt) |
| `MXBRIDGE_CONFIG` | Path to config file (default: `config.yaml`) |

### First-run setup

If `access_token` is missing but `password` is provided, the bridge will:

1. Prompt for a master password (or use `MXBIRDGE_MASTER_KEY`)
2. Log in to the Matrix server
3. Encrypt the access token and write it back to `config.yaml`
4. Offer to import an E2EE key file

## Usage

### A → B (automatic)

All messages from Server A rooms appear in the aggregation room on Server B:

```
[#general] Alice: Hello everyone
[#dev] Bob: The build is passing
📞 Alice started a voice call in [#general]
```

### B → A (reverse reply)

```
!send #general Hi from Server B!
```

Or use your Matrix client's reply feature directly on a forwarded message.

### Control commands

| Command | Description |
|---------|-------------|
| `!login` | Connect to Server A and resume forwarding |
| `!logout` | Disconnect from Server A |
| `!pause` | Pause forwarding (messages still saved) |
| `!resume` | Resume forwarding |
| `!status` | Show connection and forwarding status |

## CLI Tools

```bash
# Import last 30 days of history
python3 backfill.py

# Import specific rooms, last 7 days
python3 backfill.py --rooms "#general:a.com,!abc:a.com" --days 7

# Repair corrupted media files
python3 repair_media.py

# Encrypt/decrypt config values
python3 encrypt_tool.py encrypt
python3 encrypt_tool.py decrypt
```

## Documentation

| File | Description |
|------|-------------|
| [USERGUIDE.md](USERGUIDE.md) | Full user guide with troubleshooting |
| [USERGUIDE_CN.md](USERGUIDE_CN.md) | 用户指南（中文） |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Architecture and code documentation |
| [ARCHITECTURE_CN.md](ARCHITECTURE_CN.md) | 架构文档（中文） |
| [CHANGELOG.md](CHANGELOG.md) | Version history |

## Important Notes

- **Do not delete `store/`** — it contains E2EE keys. Loss means the bridge must be re-trusted.
- **Do not change `device_id`** after assignment — it creates a new device requiring re-verification.
- **The web interface has no TLS** — use a reverse proxy (Nginx, Caddy) for remote access.

## License

MIT
