# Matrix Bridge — 用户指南

## 概述

Matrix Bridge 是一个自托管服务，用于在两个 Matrix 服务器之间转发消息。它监控**服务器 A** 上的所有房间，并将消息转发到**服务器 B** 上的单个聚合房间，支持通过命令和 Matrix reply-to 线程进行反向回复。

### 运行模式

| 模式 | 描述 |
|------|------|
| **桥接模式**（默认） | 从 A → B 转发消息，支持反向回复和控制命令 |
| **备份模式** | 将所有消息 + 媒体保存到本地 SQLite 存储，不进行转发 |

### 功能特性

| 功能 | 描述 |
|------|------|
| 文本转发 | 所有文本、通知和表情消息从 A → B |
| 媒体转发 | 图片、视频、音频和文件（下载 → 重新上传） |
| 编辑转发 | A 上的消息编辑会自动应用到 B 上转发的消息 |
| 撤回转发 | A 上的消息撤回会自动应用到 B 上转发的消息 |
| 通话通知 | 通话开始 / 接听 / 结束事件以通知形式转发 |
| 反向回复 | B 上的用户可以通过 `!send` 命令向 A 发送消息 |
| Reply-to 支持 | B 上的用户可以直接回复转发的消息（Matrix 线程） |
| 控制命令 | `!login`、`!logout`、`!pause`、`!resume`、`!status` 用于运行时控制 |
| E2EE 支持 | 解密服务器 A 上的加密房间（通过 matrix-nio） |
| 配置加密 | 使用主密码加密敏感配置值（访问令牌、密码） |
| 加密数据库 | 使用 SQLCipher 加密的 SQLite 数据库，密钥从主密码派生 |
| 消息存储 | 基于 SQLite 的消息持久化，支持全文搜索 |
| Web 界面 | 可搜索的 Web UI，用于浏览存储的消息 |
| 状态持久化 | 重启后不重复处理旧消息 |
| 日志文件轮转 | 可选的基于文件轮转的日志记录 |

## 前提条件

- Python 3.11+
- `libsqlcipher-dev`（Debian/Ubuntu）或 `sqlcipher-dev`（Alpine）— 加密数据库所需
- 两个 Matrix 账户（每个服务器一个），供桥接使用
- 桥接账户必须已被**邀请并加入**相关房间：
  - 服务器 A 账户：加入所有要转发的房间
  - 服务器 B 账户：加入聚合房间

## 安装

```bash
cd /home/rocky/matrix
pip install -r requirements.txt
```

## 配置

### 1. 创建配置文件

```bash
cp config.example.yaml config.yaml
```

### 2. 获取访问令牌

为每个服务器获取桥接账户的访问令牌：

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

从响应中复制 `access_token` 和 `device_id`。

### 3. 编辑 `config.yaml`

#### 桥接模式示例

```yaml
logging:
  level: INFO
  file: ""                     # 空 = stdout，或设置文件路径以启用日志轮转

source:
  homeserver: "https://matrix-a.example.com"
  user_id: "@bridge-bot:a.example.com"
  access_token: "syt_xxxxx..."          # 从步骤 2 获取
  device_id: ""                           # 留空自动分配
  store_path: "./store/source"
  handle_encrypted: true
  media_max_size: 52428800               # 50 MB

target:
  homeserver: "https://matrix-b.example.com"
  user_id: "@bridge-bot:b.example.com"
  access_token: "syt_yyyyy..."
  device_id: ""                           # 留空自动分配
  store_path: "./store/target"
  handle_encrypted: true
  target_room: "!your-aggregation-room:b.example.com"

bridge:
  command_prefix: "!send"
  message_format: "[{room_name}] {sender}: {text}"
  state_path: "state.json"
  admin_users: []                        # 空 = 任何用户都可以发出命令
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
    password: ""                          # 空 = 仅限 localhost
    trusted_proxy: false
```

#### 备份模式示例

```yaml
logging:
  level: INFO

source:
  homeserver: "https://matrix.example.com"
  user_id: "@backup-bot:example.com"
  access_token: "syt_xxxxx..."
  device_id: ""                           # 留空自动分配
  store_path: "./store/source"
  handle_encrypted: true

# 无 "target" 段 — 激活备份模式

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

### 4. 加密敏感配置值（可选）

所有敏感配置值在首次运行时会自动加密。也可以手动加密：

```bash
python3 scripts/encrypt_tool.py encrypt
```

这将提示输入值和主密码，然后输出 `enc:...` 字符串。用加密值替换 `config.yaml` 中的明文：

```yaml
source:
  access_token: "enc:AAAA..."      # 加密值
  password: ""                      # 不再需要
```

启动时，桥接将提示输入主密码来解密这些值。也可以通过设置 `MXBRIDGE_MASTER_KEY` 环境变量来跳过交互式提示：

> **注意：** 主密码在启动时**始终需要**。它用于配置字段解密和数据库加密。明文凭据会在首次运行时自动加密。

```bash
export MXBRIDGE_MASTER_KEY="your-master-password"
python3 main.py
```

### 配置参考

| 字段 | 必需 | 描述 |
|------|------|------|
| `logging.level` | 否 | 日志级别：DEBUG、INFO、WARNING、ERROR（默认：`INFO`） |
| `logging.file` | 否 | 日志文件路径。空 = 仅 stdout（默认：`""`） |
| `logging.max_bytes` | 否 | 轮转前的最大日志文件大小（默认：10MB） |
| `logging.backup_count` | 否 | 保留的轮转日志文件数量（默认：3） |
| `source.homeserver` | 是 | 服务器 A 的基础 URL |
| `source.user_id` | 是 | A 上桥接账户的完整用户 ID |
| `source.access_token` | 是* | 访问令牌（或使用 `password` 首次登录） |
| `source.password` | 否 | 仅首次运行时使用以获取访问令牌 |
| `source.device_id` | 否 | 留空让服务器自动分配（会写回配置文件）。建议固定以确保 E2EE 密钥一致性 |
| `source.store_path` | 是 | E2EE 加密存储目录（必须持久化） |
| `source.handle_encrypted` | 否 | 解密加密房间（默认：`true`） |
| `source.media_max_size` | 否 | 最大媒体文件大小（字节，默认：50 MB） |
| `source.key_import_file` | 否 | E2EE 密钥导出文件路径（例如从 Element 导出） |
| `source.key_import_passphrase` | 否 | 密钥导出文件的口令 |
| `target.homeserver` | 是** | 服务器 B 的基础 URL |
| `target.user_id` | 是** | B 上桥接账户的完整用户 ID |
| `target.access_token` | 是** | 服务器 B 的访问令牌 |
| `target.target_room` | 是** | 服务器 B 上聚合消息的房间 ID |
| `bridge.command_prefix` | 否 | 反向回复命令的前缀（默认：`!send`） |
| `bridge.message_format` | 否 | A→B 消息的格式模板 |
| `bridge.admin_users` | 否 | 授权发出控制命令的 MXID 列表（空 = 任何人） |
| `bridge.media.enabled` | 否 | 转发媒体文件（默认：`true`） |
| `bridge.call_notifications.enabled` | 否 | 转发通话通知（默认：`true`） |
| `bridge.message_store.enabled` | 否 | 启用 SQLite 消息持久化（默认：`false`） |
| `bridge.message_store.path` | 否 | SQLite 数据库文件路径（默认：`messages.db`） |
| `bridge.message_store.media_dir` | 否 | 保存媒体文件的本地目录（默认：`./media`） |
| `bridge.web.enabled` | 否 | 启用 Web 搜索界面（默认：`false`） |
| `bridge.web.host` | 否 | Web 服务器绑定主机（默认：`0.0.0.0`） |
| `bridge.web.port` | 否 | Web 服务器绑定端口（默认：`8080`） |
| `bridge.web.password` | 否 | Web 访问密码。空 = 仅限 localhost |
| `bridge.web.trusted_proxy` | 否 | 信任 X-Forwarded-For 请求头（默认：`false`） |

> *必须提供 `access_token` 或 `password` 之一。
>
> **桥接模式必需。省略整个 `target` 段以使用备份模式。

## 运行

### 前台运行（测试用）

```bash
python3 main.py
```

### 使用自定义配置路径

```bash
python3 main.py /path/to/config.yaml
```

### 后台运行（生产环境）

```bash
nohup python3 main.py > bridge.log 2>&1 &
```

### 使用 systemd

创建 `/etc/systemd/system/matrix-bridge.service`：

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

### 首次运行交互式设置

主密码在启动时**始终需要**（通过 `MXBRIDGE_MASTER_KEY` 环境变量或交互式提示）。它用于：
1. 解密加密的配置字段（`enc:` 前缀字段）
2. 派生 `messages.db` 的 SQLCipher 加密密钥
3. 自动加密配置中发现的明文凭据

如果配置中未提供 `access_token` 但提供了 `password`，桥接将：
1. 使用提供的密码登录 Matrix 服务器
2. 加密收到的访问令牌并写回 `config.yaml`
3. 提供导入 E2EE 密钥文件的选项

后续启动时，只需输入主密码（或设置 `MXBRIDGE_MASTER_KEY`）。盐文件（`messages.db.salt`）会自动生成以派生数据库加密密钥 — **不要删除此文件**。

## 使用方法

### A → B（自动）

服务器 A 房间中的所有消息会出现在服务器 B 的聚合房间中：

```
[#general] Alice: Hello everyone
[#dev] Bob: The build is passing
📞 Alice started a voice call in [#general]
📞 voice call ended in [#general]
```

在 A 上编辑的消息会自动在 B 上编辑。在 A 上撤回的消息会自动在 B 上撤回。

### B → A（回复命令）

服务器 B 上的用户在聚合房间中发送命令：

```
!send #general Hi from Server B!
!send !abc123:server-a.com 也可以使用直接的房间 ID
```

消息将作为桥接机器人发送到服务器 A 上的指定房间，并在命令消息上添加 ✓ 表情回应。

### B → A（Reply-to）

服务器 B 上的用户可以使用 Matrix 客户端的回复功能直接回复聚合房间中的任何转发消息。回复将自动路由到正确的源房间。

### 控制命令

以下命令可在服务器 B 的聚合房间中使用：

| 命令 | 描述 |
|------|------|
| `!login` | 连接到服务器 A 并恢复转发 |
| `!logout` | 断开与服务器的连接并暂停转发 |
| `!pause` | 暂停转发（源保持连接，消息仍保存） |
| `!resume` | 暂停后恢复转发 |
| `!status` | 显示当前连接和转发状态 |

**启动行为：** 桥接启动时仅连接服务器 B。如果源之前已登出，使用 `!login` 连接。如果之前是活跃的，会自动重新连接。

**授权：** 如果设置了 `bridge.admin_users`，只有列表中的 MXID 可以发出命令。如果为空，目标房间中的任何用户都可以发出命令。

### 命令语法

```
!send <room_alias_or_id> <message text>
```

| 参数 | 描述 |
|------|------|
| `room_alias_or_id` | 房间别名如 `#general:a.example.com` 或房间 ID 如 `!abc123:a.example.com` |
| `message text` | 剩余部分为消息内容 |

## Web 界面

当 `bridge.web.enabled` 为 `true` 时，可以使用 Web UI 搜索和浏览存储的消息。

### 访问

- 如果设置了 `web.password`：访问 `http://your-server:8080` 并使用密码登录
- 如果 `web.password` 为空：仅可从 `http://127.0.0.1:8080` 访问（自动认证）

### 功能

- 所有存储消息的全文搜索
- 按房间、发送者和日期范围过滤
- 浏览房间消息历史
- 查看消息上下文（周围的消息）
- 查看和下载媒体文件
- 统计仪表板（总消息数、房间数、日期范围）
- **历史回填** — 通过 Web 界面从源服务器导入历史消息

### 从 Web 界面回填历史

点击页眉中的 **Backfill** 按钮即可导入历史消息：

1. **历史天数**：要获取的天数（0 = 所有可用历史）
2. **下载媒体文件**：是否下载并保存媒体附件
3. **回填前清空数据库和媒体**：可选地清除所有现有数据并重新下载

进度会实时显示，逐房间更新状态。同一时间只能运行一个回填任务。已存在的消息会自动跳过（不会重复）。

### 反向代理

对于带 TLS 的远程访问，将 Web 界面放在反向代理后面：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
}
```

在 Web 配置中设置 `trusted_proxy: true` 以从 `X-Forwarded-For` 读取真实客户端 IP。

## CLI 工具

### Backfill — 导入历史消息

```bash
# 从所有房间导入最近 30 天的消息
python3 scripts/backfill.py

# 导入指定房间，最近 7 天
python3 scripts/backfill.py --rooms "#general:a.com,!abc:a.com" --days 7

# 模拟运行（显示将要导入的内容）
python3 scripts/backfill.py --dry-run

# 跳过媒体下载
python3 scripts/backfill.py --no-media

# 限制最多 1000 条消息
python3 scripts/backfill.py --limit 1000
```

### Repair media — 修复损坏的文件

```bash
# 检查并修复损坏的媒体文件
python3 scripts/repair_media.py

# 模拟运行（显示将要修复的内容）
python3 scripts/repair_media.py --dry-run
```

### Encrypt tool — 加密/解密配置值

```bash
python3 scripts/encrypt_tool.py encrypt    # 加密一个值
python3 scripts/encrypt_tool.py decrypt    # 解密一个值
```

## 密钥导入

要解密加密房间中的历史消息，可以导入从其他客户端（例如 Element）导出的 Megolm 会话密钥：

```yaml
source:
  key_import_file: "/path/to/keys.txt"
  key_import_passphrase: "你使用的口令"
```

密钥在启动时导入。成功导入后，可以从配置中移除这些字段。导入也支持在交互式首次运行设置期间进行。

## 日志记录

默认情况下，日志输出到 stdout。要启用基于文件轮转的日志记录：

```yaml
logging:
  level: INFO
  file: "/var/log/matrix-bridge/bridge.log"
  max_bytes: 10485760      # 10 MB
  backup_count: 3           # 保留 3 个轮转文件
```

## 故障排除

### 消息未出现在服务器 B 上

- 检查服务器 A 上的桥接账户是否已加入源房间
- 检查 `target_room` 是否正确，且服务器 B 上的桥接账户是否已加入
- 检查转发是否暂停 — 在目标房间发送 `!status`
- 检查日志中的同步错误

### E2EE 消息未解密

- 确保 `handle_encrypted: true`
- 确保 `device_id` 自首次运行以来未更改
- 确保 `store_path` 目录存在且可写
- 桥接账户必须在消息发送时已在房间中
- 尝试通过 `key_import_file` / `key_import_passphrase` 导入密钥
- 如果消息在机器人加入前发送，可能需要重新共享密钥

### 目标房间中出现"无法解密"通知

- 这意味着缺少 Megolm 会话密钥
- 桥接自动排队这些事件，并在密钥到达时重试
- 失败的解密在重启间持久化
- 检查发送者的设备是否已验证且密钥已共享

### 重启后消息重复

- 状态现在持久化在 SQLite 数据库中（以前是 `state.json`）。如果数据库被删除，桥接将重新处理旧消息。
- 升级后首次运行时，`state.json` 会自动迁移到数据库并删除。

### 数据库从明文迁移到加密

- 如果之前有明文 `messages.db`，首次运行时会自动迁移到 SQLCipher
- 明文数据库的备份保留在 `messages.db.plaintext.bak`
- 验证迁移成功后，手动删除备份文件

### 媒体文件未转发

- 检查 `media_max_size` — 大文件会被静默跳过
- 检查 `bridge.media.enabled: true`
- 检查磁盘空间和两个服务器之间的网络连接

### Web 界面无法访问

- 如果 `web.password` 为空，界面仅绑定到 `127.0.0.1`
- 设置密码后将绑定到配置的主机
- 对于远程访问，使用带 TLS 的反向代理

### 启动时配置解密失败

- 确保输入了正确的主密码
- 如果密码丢失，需要重新加密凭据并**重建数据库**：
  1. 删除 `messages.db`、`messages.db.salt` 和 `messages.db.plaintext.bak`（如果有）
  2. 获取新的访问令牌
  3. 使用 `scripts/encrypt_tool.py encrypt` 和新主密码加密
  4. 用新的加密值更新 `config.yaml`

## 重要说明

- **不要删除 `store/` 目录** — 它们包含 E2EE 密钥。删除意味着桥接失去所有解密能力，需要被其他用户重新信任。
- **不要更改已分配的 `device_id`** — 更改会创建新设备，需要重新验证。首次运行时在配置中留空，服务器会自动分配一个。
- **不要删除 `messages.db.salt`** — 它是派生数据库加密密钥所必需的。丢失意味着数据库无法读取。
- **不要丢失主密码** — 它同时用于配置解密和数据库访问。丢失意味着所有加密数据无法恢复。
- **服务器 A 上的桥接账户将显示为未验证设备**。其他用户可以在其客户端中验证（Element：设置 → 安全 → 验证设备）以消除警告。
- **`!pause` 期间的消息会被保存但不会转发。** 当 `!resume` 时，仅转发新消息 — 暂停期间的消息已保存到存储中。
- **`!logout` 会清除所有事件映射。** 使用 `!login` 重新登录后，之前转发的消息无法被追溯编辑/撤回。
- **Web 界面不支持 TLS。** 对于远程访问，请将其放在 TLS 终止的反向代理（如 Nginx、Caddy）后面。切勿直接暴露到互联网。
