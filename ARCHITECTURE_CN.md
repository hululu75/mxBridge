# Matrix Bridge — 技术架构

## 1. 系统概述

### 桥接模式

```
┌──────────────────┐         ┌─────────────────┐         ┌──────────────────┐
│   Matrix 服务器 A │         │    桥接核心       │         │  Matrix 服务器 B  │
│                  │         │                  │         │                  │
│  ┌────────────┐  │  sync   │  ┌────────────┐  │  sync   │  ┌────────────┐  │
│  │  房间       │◄─┼─────────┼─►│  Source     │  │────────►│  │ 聚合房间    │  │
│  │  (多个)     │  │         │  │  Backend    │  │         │  │ (单个)     │  │
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
                 │  状态存储    │ │ 消息存储     │ │  Web 服务   │
                 │ state.json  │ │ SQLite DB   │ │ (aiohttp)  │
                 └─────────────┘ └────────────┘ └────────────┘
```

### 备份模式（无目标服务器）

```
┌──────────────────┐         ┌─────────────────┐
│   Matrix 服务器   │         │    桥接核心       │
│                  │  sync   │  (备份模式)       │
│  ┌────────────┐  │◄────────┼─►│  Source     │  │
│  │  房间       │  │         │  │  Backend    │  │
│  │  (多个)     │  │         │  └──────┬─────┘  │
│  └────────────┘  │         │         │        │
└──────────────────┘         │         ▼        │
                             │  ┌────────────┐  │
                             │  │ 消息存储     │  │
                             │  │ SQLite DB   │  │
                             │  └────────────┘  │
                             └─────────────────┘
```

桥接程序作为长期运行的进程，以独立客户端身份连接到一个或两个 Matrix 服务器。无需对服务器进行任何修改。

支持两种运行模式：
- **桥接模式**（默认）：从源服务器转发消息到目标服务器，支持反向回复和控制命令。
- **备份模式**：当配置中缺少 `target` 段时，将所有消息和媒体保存到本地 SQLite 存储，不进行转发。

---

## 2. 项目结构

```
matrix/
├── main.py                       # 入口：配置加载、加密、信号处理、启动
├── config.example.yaml           # 配置模板
├── requirements.txt              # Python 依赖
├── encrypt_tool.py               # CLI：加密/解密配置值
├── backfill.py                   # CLI：导入历史消息到 MessageStore
├── repair_media.py               # CLI：修复损坏的加密媒体文件
├── bridge/
│   ├── __init__.py
│   ├── models.py                 # BridgeMessage 数据类 — 统一的跨后端消息模型
│   ├── core.py                   # BridgeCore — 消息路由、备份模式、控制命令
│   ├── state.py                  # StateManager — 同步令牌、事件去重、转发状态、事件映射
│   ├── message_store.py          # MessageStore — 基于 SQLite 的消息持久化（含 FTS5）
│   ├── web.py                    # WebServer — aiohttp HTTP API，用于消息搜索和浏览
│   ├── crypto.py                 # 配置字段加密/解密（Fernet + PBKDF2）
│   └── templates/
│       ├── index.html            # Web UI 单页应用
│       └── marked.min.js         # Markdown 渲染库
├── backends/
│   ├── __init__.py
│   ├── base.py                   # BaseBackend — 所有协议适配器的抽象接口
│   ├── matrix_base.py            # MatrixBackend — 共享的 Matrix 客户端逻辑（认证、同步、媒体、密钥）
│   ├── matrix_source.py          # MatrixSourceBackend — 监控所有房间，发送 FORWARD/EDIT/REDACT
│   └── matrix_target.py          # MatrixTargetBackend — 聚合房间、回复、控制命令
└── store/                        # 运行时 E2EE 密钥存储（自动创建）
    ├── source/                   # 服务器 A 连接的 Olm/Megolm 密钥
    └── target/                   # 服务器 B 连接的 Olm/Megolm 密钥
```

---

## 3. 模块规格

### 3.1 `bridge/models.py` — 统一消息模型

表示任何来源协议消息的单一数据类。

```python
class MessageDirection(str, Enum):
    FORWARD = "forward"       # A → B 消息
    REPLY = "reply"           # B → A 回复（通过 !send 命令或 reply-to）
    CONTROL = "control"       # 桥接控制命令（!login、!logout 等）
    REDACT = "redact"         # 来自源端的撤回事件
    EDIT = "edit"             # 来自源端的编辑事件

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
    # 身份信息
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

    # 路由信息
    target_room_id: str | None
    target_room_name: str | None

    # 媒体字段
    media_url: str | None
    media_data: bytes | None
    media_mimetype: str | None
    media_filename: str | None
    media_size: int | None
    thumbnail_url: str | None
    media_width / media_height / media_duration: int | None

    # 通话通知字段
    call_type: str | None         # "voice" | "video"
    call_action: CallAction | None  # STARTED | ANSWERED | ENDED
    call_duration: int | None
    call_callee: str | None
    call_join_url: str | None

    # 编辑 / 撤回 / 回复追踪
    from_self: bool                 # 发送者是桥接机器人本身时为 True
    edit_of_event_id: str | None   # 引用被编辑的原始事件
    reply_to_event_id: str | None  # Matrix reply-to 事件 ID
    redacted_event_id: str | None  # 被撤回的事件 ID

    # 可扩展性
    extra_content: dict
```

---

### 3.2 `backends/base.py` — 抽象后端接口

所有协议适配器必须实现此接口：

```python
class BaseBackend(ABC):
    # 生命周期
    async def start(self) -> None
    async def stop(self) -> None

    # 发送
    async def send_message(room_id, text, msgtype) -> str
    async def send_media(room_id, data, mimetype, filename, msgtype, extra_info) -> str
    async def redact_event(room_id, event_id, reason) -> str
    async def edit_message(room_id, event_id, new_text, msgtype) -> str
    async def resolve_room_id(room_alias_or_id) -> str | None

    # 事件发射
    def on_message(callback)
    async def _emit_message(message)
```

**添加新协议**（例如 Telegram、Discord、Teams）：
1. 创建 `backends/telegram.py`，继承 `BaseBackend`
2. 实现所有抽象方法
3. 在 `start()` 中，收到消息时调用 `self._emit_message(BridgeMessage(...))`
4. 在 `config.yaml` 中添加 `type: "telegram"` 条目
5. 更新 `main.py`，根据 `type` 实例化正确的后端

---

### 3.3 `backends/matrix_base.py` — 共享 Matrix 客户端逻辑

`MatrixBackend(BaseBackend)` 包含源和目标后端共享的所有 Matrix 客户端逻辑。提取此模块是为了避免 `matrix_source.py` 和 `matrix_target.py` 之间的代码重复。

#### 关键组件

| 组件 | 描述 |
|------|------|
| `_init_client()` | 创建 `AsyncClient`，认证（令牌或密码），上传密钥，导入密钥，验证连接 |
| `_sync_loop()` | 长轮询同步，带自动密钥维护和 to-device 消息刷新 |
| `_download_media()` | 从 `mxc://` URI 下载媒体，支持大小限制和 E2EE 解密 |
| `_import_keys_if_configured()` | 从导出文件导入 Megolm 会话密钥（Element 密钥导出） |
| `_persist_device_id()` | 将服务器分配的 `device_id` 写回配置 YAML 文件 |
| `_register_common_callbacks()` | 注册 SAS 密钥验证自动接受和房间密钥监听器 |
| `_enqueue_pending_encrypted()` | 将解密失败的事件排队，等待密钥到达后自动重试 |
| `_recheck_pending_keys()` | 定期重新请求排队事件缺失的房间密钥 |

#### 认证流程

```
_init_client()
    │
    ├─ 已提供 access_token？ ──► restore_login()
    │
    └─ 无令牌 ──► 使用密码登录
                     │
                     ├─ 服务器分配了 device_id？ ──► _persist_device_id()
                     │
                     └─ keys_upload() → keys_query() → _import_keys_if_configured()
```

#### 待处理加密事件队列

当 Megolm 事件无法解密（缺少会话密钥）时，事件将被排队：

1. 事件 + 房间存储在 `_pending_encrypted[session_id]` 中
2. 立即通过 `request_room_key()` 请求房间密钥
3. 查询并声明发送者的设备密钥
4. 后台任务（`_periodic_key_upload`）每 120 秒重新请求密钥
5. 当房间密钥到达（`_on_room_key_received`）时，排队的事件被解密并分发
6. 通过 `StateManager._failed_decryptions` 在重启间持久化

最大队列大小：200 个会话（`MAX_PENDING_SESSIONS`）。

#### SAS 密钥验证

桥接程序自动接受并完成来自其他用户的交互式 SAS 密钥验证请求：

```
KeyVerificationStart ──► accept_key_verification()
KeyVerificationKey    ──► confirm_short_auth_string()
KeyVerificationMac    ──► 标记为已验证，发送 m.key.verification.done
```

同时处理 `m.key.verification.request` to-device 事件，以 `m.key.verification.ready` 响应。

---

### 3.4 `backends/matrix_source.py` — 服务器 A 后端

**职责：** 连接到服务器 A，监控所有已加入的房间，发送 `FORWARD`、`EDIT` 和 `REDACT` 消息。

#### 初始化（`start()`）

1. 调用 `_init_client()` 进行认证和密钥设置
2. 从 `StateManager` 恢复同步位置
3. 使用 `full_state=True` 执行初始同步以加载房间状态
4. 注册事件回调：`RoomMessage`、`CallInviteEvent`、`CallAnswerEvent`、`CallHangupEvent`、`MegolmEvent`、`RedactionEvent`
5. 查询所有加密房间成员的设备密钥
6. 从上次运行加载失败的解密会话
7. 启动后台任务：定期刷新、密钥上传、通话清理、同步循环

#### 事件处理流水线

```
传入事件（同步响应）
    │
    ├─ event_id 已处理？ ──► 跳过（去重）
    │
    ├─ RoomMessageText ──► 检查 m.relates_to
    │     ├─ rel_type == "m.replace" ──► BridgeMessage(direction=EDIT, edit_of_event_id=...)
    │     └─ 普通文本 ──► BridgeMessage(direction=FORWARD, msgtype=TEXT)
    │
    ├─ RoomMessageNotice ──► BridgeMessage(direction=FORWARD, msgtype=NOTICE)
    ├─ RoomMessageEmote ──► BridgeMessage(direction=FORWARD, msgtype=EMOTE)
    ├─ RoomMessageImage ──► 下载媒体 ──► BridgeMessage(direction=FORWARD, msgtype=IMAGE)
    ├─ RoomMessageVideo ──► 下载媒体 ──► BridgeMessage(direction=FORWARD, msgtype=VIDEO)
    ├─ RoomMessageAudio ──► 下载媒体 ──► BridgeMessage(direction=FORWARD, msgtype=AUDIO)
    ├─ RoomMessageFile ──► 下载媒体 ──► BridgeMessage(direction=FORWARD, msgtype=FILE)
    │
    ├─ RedactionEvent ──► BridgeMessage(direction=REDACT, redacted_event_id=...)
    │
    ├─ CallInviteEvent ──► 解析 SDP ──► BridgeMessage(msgtype=CALL_NOTIFICATION, call_action=STARTED)
    ├─ CallAnswerEvent ──► BridgeMessage(msgtype=CALL_NOTIFICATION, call_action=ANSWERED)
    ├─ CallHangupEvent ──► BridgeMessage(msgtype=CALL_NOTIFICATION, call_action=ENDED)
    │
    └─ MegolmEvent ──► 解密
          ├─ 成功 ──► 将解密后的事件重新分发到流水线
          └─ 失败 ──► _enqueue_pending_encrypted()（密钥到达后重试）
```

#### 编辑检测

包含 `m.relates_to.rel_type == "m.replace"` 和 `m.new_content` 的消息被检测为编辑。编辑后的文本从 `m.new_content.body` 中提取，以 `direction=EDIT` 发送，`edit_of_event_id` 引用原始事件。

#### 撤回处理

来自其他用户（非桥接本身）的 `RedactionEvent` 事件以 `direction=REDACT` 发送，`redacted_event_id` 设置为被撤回的事件。

#### 房间密钥重试（Source 特有）

重写 `_before_key_rerequest()` 以在重新请求前调用 `cancel_key_share()`，以及 `_on_pending_encrypted_enqueued()` 以将失败的解密持久化到 `StateManager`。

当房间密钥到达时，重试内存中的待处理事件和上次运行持久化的事件（通过 `room_get_event` → 解密 → 分发）。

#### 通话检测

- `CallInviteEvent`：检查 SDP offer 中的 `"video"` 关键字，分类为语音/视频
- 在 `_active_calls` 字典中跟踪活跃通话（以 `call_id` 为键）
- `CallHangupEvent`：从跟踪状态填充通话时长
- 过期的通话（超过 24 小时）每小时清理一次

#### 媒体下载

- 通过 `client.download(mxc=...)` 下载
- 对于加密媒体（`RoomEncryptedMedia`），使用 `decrypt_attachment()` 解密
- 遵循 `media_max_size` 配置（默认 50 MB）
- 如果文件超出限制，`media_data` 设为 `None`

---

### 3.5 `backends/matrix_target.py` — 服务器 B 后端

**职责：** 连接到服务器 B，监控聚合房间，解析回复命令和控制命令，检测 reply-to 消息。

#### 初始化

1. 调用 `_init_client()` 进行认证
2. 注册回调：`RoomMessage`、`MegolmEvent`、通用回调
3. 使用 `full_state=True` 执行初始同步以加载房间状态
4. 恢复同步位置
5. 查询目标房间成员的设备密钥
6. 启动同步循环，带 `_after_sync` 钩子用于未解密事件检测

#### 事件处理

```
目标房间中的传入消息
    │
    ├─ 来自自身设备？ ──► 跳过（防止循环）
    ├─ 不在 target_room 中？ ──► 跳过
    ├─ event_id 已处理？ ──► 跳过（去重）
    │
    ├─ 包含 m.in_reply_to？ ──► BridgeMessage(direction=REPLY, reply_to_event_id=...)
    │
    ├─ 匹配控制命令？ ──► BridgeMessage(direction=CONTROL, text="login"|"logout"|...)
    │   命令：!login、!logout、!pause、!resume、!status
    │
    ├─ 以 command_prefix 开头？ ──► 解析 "!send #room message"
    │     ├─ 有效 ──► BridgeMessage(direction=REPLY, target_room_id=..., text=...)
    │     └─ 无效 ──► 发送使用帮助
    │
    └─ 媒体事件 ──► 下载媒体 ──► BridgeMessage(direction=REPLY, msgtype=IMAGE/...)
```

#### Reply-to 支持

当用户在目标房间中回复转发的消息时，后端：
1. 从事件内容中提取 `m.in_reply_to.event_id`
2. 从消息体中去除 Matrix 回复引用块
3. 发送 `REPLY` 消息，设置 `reply_to_event_id`
4. `BridgeCore` 将回复解析到正确的源房间

#### 控制命令路由

控制命令从 `command_prefix` 的第一个字符派生：
- 默认前缀 `!send` → 控制前缀 `!`
- 命令：`!login`、`!logout`、`!pause`、`!resume`、`!status`

通过去除首尾空白的消息体的精确字符串匹配来检测。

#### 未解密事件检测

每次同步后，`_check_undecrypted_events()` 扫描目标房间时间线中无法解密的 `MegolmEvent` 条目。对每个发送通知：`"⛔ Unable to decrypt message from {sender}"`，并将事件排队重试。

#### 额外方法

| 方法 | 描述 |
|------|------|
| `get_event_body(room_id, event_id)` | 通过 `room_get_event` API 获取事件正文 |
| `send_reaction(room_id, event_id, key)` | 发送 `m.reaction` 注释（默认键：✓） |
| `send_message(room_id, text, msgtype)` | 重写默认 msgtype 为 `m.notice` |

---

### 3.6 `bridge/core.py` — 消息路由器

**职责：** 连接源和目标后端，路由消息，处理控制命令，管理备份模式。

#### 初始化

```python
class BridgeCore:
    _source: BaseBackend
    _target: Optional[BaseBackend]     # 备份模式下为 None
    _backup_mode: bool                 # 当 target 为 None 时为 True
    _store: Optional[MessageStore]     # SQLite 持久化层
    _forwarding_enabled: bool          # 由 !login/!logout 切换
    _forwarding_paused: bool           # 由 !pause/!resume 切换
    _admin_users: set[str]             # 授权的命令用户
    _source_to_target_map: dict        # source_event_id → target_event_id
    _room_id_map: dict                 # target_event_id → source_room_id
```

#### 启动行为

```
桥接模式：
    1. 启动目标后端
    2. 检查保存的状态：上次 forwarding_enabled 是否为 True？
       ├─ 是 ──► 启动源后端，转发激活
       └─ 否 ──► 跳过源，等待 !login 命令
    3. 从状态检查 forwarding_paused

备份模式：
    1. 立即启动源后端
    2. 设置 forwarding_enabled = True
    3. 所有消息保存到存储，不转发
```

#### A → B 转发（`_on_source_message`）

```
来自源的 BridgeMessage
    │
    ├─ direction == REDACT ──► _on_source_redact()
    ├─ direction == EDIT   ──► _on_source_edit()
    ├─ direction != FORWARD ──► 跳过
    │
    ├─ 保存到 MessageStore（如果启用）
    ├─ from_self == True ──► 跳过（不转发自己的消息）
    ├─ backup_mode ──► 跳过（不转发）
    ├─ !forwarding_enabled || forwarding_paused ──► 跳过
    │
    ├─ msgtype == CALL_NOTIFICATION ──► _forward_call_notification()
    ├─ msgtype in [IMAGE, VIDEO, AUDIO, FILE] AND media_data ──► _forward_media()
    └─ msgtype in [TEXT, NOTICE, EMOTE] ──► _forward_text()
```

#### 编辑转发（`_on_source_edit`）

1. 在 MessageStore 中更新消息文本
2. 通过 `_source_to_target_map` 查找目标事件 ID
3. 如果找到，调用 `target.edit_message()` 编辑转发的消息
4. 暂停期间或来自自身的编辑不会被转发

#### 撤回转发（`_on_source_redact`）

1. 从 MessageStore 中删除消息
2. 通过 `_source_to_target_map` 查找目标事件 ID
3. 如果找到，调用 `target.redact_event()` 在服务器 B 上撤回
4. 从状态中移除映射

#### B → A 回复（`_on_target_message`）

```
来自目标的 BridgeMessage
    │
    ├─ backup_mode ──► 跳过
    ├─ direction == CONTROL ──► _handle_control()
    ├─ direction != REPLY ──► 跳过
    │
    ├─ 有 reply_to_event_id？ ──► 解析源房间
    │     ├─ 检查 _room_id_map（内存中）
    │     └─ 回退：获取事件正文，解析 [room_name] 前缀
    │
    ├─ 有 target_room_id？ ──► 解析房间别名 → 房间 ID
    │
    └─ 转发到源端：
          ├─ 媒体？ ──► source.send_media()
          └─ 文本？ ──► source.send_message()
          然后：target.send_reaction(✓) 在回复事件上
```

#### 控制命令处理（`_handle_control`）

```
来自目标房间的控制消息
    │
    ├─ backup_mode ──► 跳过
    ├─ 错误的房间？ ──► 跳过
    ├─ 已设置 admin_users 且发送者不在其中？ ──► 跳过
    │
    ├─ "login"  ──► 启动源，设置 forwarding_enabled=True
    ├─ "logout" ──► 停止源，设置 forwarding_enabled=False，清除所有映射
    ├─ "pause"  ──► 设置 forwarding_paused=True（源保持连接）
    ├─ "resume" ──► 设置 forwarding_paused=False
    ├─ "status" ──► 发送状态通知（源是否连接？转发状态？）
    │
    └─ 每个命令后持久化状态
```

#### 事件映射持久化

维护两个双向映射，用于编辑/撤回/回复解析：

| 映射 | 用途 | 最大大小 |
|------|------|----------|
| `source_target_map` | source_event_id → target_event_id | 5,000 |
| `event_room_map` | target_event_id → source_room_id | 5,000 |

两者都持久化在 `state.json` 中，以 FIFO 方式淘汰。

---

### 3.7 `bridge/state.py` — 状态持久化

**存储格式：** JSON 文件（`state.json`）

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

**操作：**

| 方法 | 描述 |
|------|------|
| `load()` | 启动时读取 state.json |
| `save_sync_token(backend, token)` | 存储每个后端的同步批次令牌 |
| `load_sync_token(backend)` | 重启后恢复同步位置 |
| `is_processed(event_id)` | 检查事件是否已处理 |
| `mark_processed(event_id)` | 记录事件以防重复处理 |
| `save_event_room(event_id, room_id)` | 映射目标事件 → 源房间 |
| `get_event_room(event_id)` | 查找目标事件的源房间 |
| `save_source_target(source_id, target_id)` | 映射源事件 → 目标事件 |
| `get_target_event_id(source_id)` | 查找源事件的目标事件 |
| `pop_source_target(source_id)` | 移除并返回映射 |
| `clear_mappings()` | 清除所有事件映射（登出时） |
| `get_forwarding_enabled()` | 获取转发状态 |
| `set_forwarding_enabled(bool)` | 设置转发状态 |
| `get_forwarding_paused()` | 获取暂停状态 |
| `set_forwarding_paused(bool)` | 设置暂停状态 |
| `save_failed_decryption(session_id, room_id, event_id)` | 持久化失败的解密以供跨重启重试 |
| `pop_failed_decryptions(session_id)` | 检索并移除持久化的失败记录 |
| `flush()` | 将状态写入磁盘（如果有变更） |

**淘汰策略：**
- `processed_events`：10,000 条（FIFO）
- `event_room_map`：5,000 条（FIFO）
- `source_target_map`：5,000 条（FIFO）
- `failed_decryptions`：所有会话总计 500 条

**刷新时机：** 状态每 60 秒刷新到磁盘（周期性任务）和优雅关闭时。文件以原子方式写入（临时文件 + `os.replace`），仅限所有者权限。

---

### 3.8 `bridge/message_store.py` — SQLite 消息持久化

基于 Peewee ORM 的消息存储，支持全文搜索、别名管理和媒体文件存储。

#### 数据库 Schema

**`messages` 表：**

| 列 | 类型 | 描述 |
|----|------|------|
| `id` | AutoField (PK) | 行 ID，同时作为 FTS rowid |
| `timestamp` | DateTime | UTC 时间戳 |
| `direction` | CharField | "forward"、"reply"、"control"、"edit"、"redact" |
| `source_room_id` | CharField | |
| `source_room_name` | CharField | |
| `sender` | CharField | |
| `sender_displayname` | CharField | |
| `text` | TextField | 消息正文 |
| `msgtype` | CharField | "m.text"、"m.image" 等 |
| `event_id` | CharField (UNIQUE) | Matrix 事件 ID |
| `target_room_id` | CharField | |
| `media_url` | CharField | 原始 mxc:// URI |
| `media_filename` | CharField | |
| `media_mimetype` | CharField | |
| `media_size` | IntegerField | |
| `call_type` | CharField | "voice" / "video" |
| `call_action` | CharField | "started" / "answered" / "ended" |
| `call_duration` | IntegerField | 秒 |
| `from_self` | BooleanField | 发送者是桥接机器人时为 True |
| `media_local_path` | CharField | media_dir 内的相对路径 |
| `edit_of_event_id` | CharField (Indexed) | 引用原始事件 |

**`bridge_config` 表：** 内部状态的键值存储（例如 `web_secret`、`migrated_aliases_v1`）。

**`user_aliases` 表：** 映射 `sender_id` (PK) → `displayname`。

**`room_aliases` 表：** 映射 `room_id` (PK) → `room_name`。

**`messages_fts` 虚拟表：** `text` 列的 FTS5 全文索引，通过 INSERT/DELETE/UPDATE 触发器同步。

#### SQLite Pragmas

- `journal_mode = wal`（预写式日志）
- `busy_timeout = 5000`（5 秒锁超时）

#### 关键特性

| 特性 | 描述 |
|------|------|
| Schema 迁移 | 自动添加 `from_self`、`media_local_path`、`edit_of_event_id` 列（如果缺失） |
| 别名迁移 | 首次运行时从现有消息填充 `user_aliases` 和 `room_aliases` |
| 媒体存储 | 将文件保存到 `YYYY-MM/` 子目录，原子写入 |
| 编辑协调 | `reconcile_edits()` 解析编辑链，应用最新文本，移除编辑存根 |
| FTS5 搜索 | 全文搜索，自动触发器同步索引，回退到 LIKE |
| 去重 | `event_id` UNIQUE 约束静默捕获重复项 |
| 别名丰富 | 搜索结果将 ID 替换为最新已知的显示名 |

#### 集成点

- `bridge/core.py`：从异步线程调用 `save_message()`、`upsert_user_alias()`、`upsert_room_alias()`、`update_message_text()`、`delete_message()`
- `backfill.py`：批量导入历史消息
- `bridge/web.py`：通过 HTTP API 提供存储的消息

---

### 3.9 `bridge/web.py` — Web 搜索界面

基于 aiohttp 的 HTTP 服务器，提供可搜索的消息存储 Web 界面。

#### 端点

| 方法 | 路径 | 认证 | 描述 |
|------|------|------|------|
| GET | `/` | 无 | 提供 HTML UI（`index.html`） |
| POST | `/api/login` | 无 | 认证，返回 HMAC 签名的 bearer 令牌 |
| GET | `/api/stats` | 是 | 总消息数、房间数、转发/回复数、日期范围 |
| GET | `/api/rooms` | 是 | 列出房间，带消息数和最后消息时间戳 |
| GET | `/api/rooms/{room_id}/senders` | 是 | 列出房间内的发送者，带显示名和计数 |
| GET | `/api/search?q=&room=&sender=&from=&to=&page=&limit=` | 是 | 全文搜索，带过滤和分页 |
| GET | `/api/history/{room_id}?page=&limit=` | 是 | 分页房间消息历史（升序） |
| GET | `/api/context/{event_id}?before=&after=` | 是 | 消息及周围 N 条消息的上下文 |
| GET | `/api/media/{event_id}` | 是 | 从本地存储提供已保存的媒体文件 |
| GET | `/static/*` | 无 | `bridge/templates/` 中的静态文件 |

#### 认证

- **基于令牌**：HMAC-SHA256 签名令牌，7 天有效期
- **密钥**：自动生成的 32 字节随机密钥，持久化在 `bridge_config` 表中
- **无密码模式**：如果 `web.password` 为空，仅绑定到 `127.0.0.1`，自动发放令牌
- **密码模式**：如果设置了密码，需要通过 `/api/login` 登录
- **令牌传递**：`Authorization: Bearer <token>` 请求头或 `?token=` 查询参数
- **速率限制**：每个客户端 IP 每 60 秒 10 次登录尝试

#### 安全

- **反向代理支持**：`trusted_proxy` 标志从 `X-Forwarded-For` 读取真实 IP
- **路径遍历保护**：媒体服务验证解析路径保持在 `media_dir` 内
- **HMAC 常量时间比较**：令牌和密码检查使用 `hmac.compare_digest()`

---

### 3.10 `bridge/crypto.py` — 配置字段加密

用于敏感配置值（访问令牌、密码、密钥口令）的对称加密。

| 函数 | 描述 |
|------|------|
| `encrypt(plaintext, master_password)` | 加密值，返回 `enc:...` 前缀的字符串 |
| `decrypt(encrypted_value, master_password)` | 解密 `enc:...` 值 |
| `is_encrypted(value)` | 检查值是否以 `enc:` 前缀开头 |
| `decrypt_config(config, master_password)` | 遍历 source/target 段，解密所有加密字段 |

**加密细节：**
- 算法：Fernet（AES-128-CBC，带 HMAC-SHA256 认证）
- 密钥派生：PBKDF2-HMAC-SHA256，600,000 次迭代，16 字节随机盐
- 加密值以 `enc:` 前缀标识
- 支持的字段：`access_token`、`password`、`key_import_passphrase`

---

### 3.11 `main.py` — 入口点

```
1. 加载 config.yaml
2. 设置日志（stdout 或轮转文件）
3. 检查加密字段（enc: 前缀）
   └─ 提示输入主密码，调用 decrypt_config()
4. 交互式凭据设置（如果缺少 access_token）
   ├─ 通过 _matrix_login() 使用密码登录
   ├─ 用主密码加密令牌
   ├─ 写回 config.yaml
   └─ 可选：导入加密密钥
5. 初始化 StateManager，加载持久化状态
6. 确定模式：桥接（有 target）或备份（无 target）
7. 初始化 MessageStore（如果 message_store.enabled）
8. 创建后端：
   ├─ MatrixSourceBackend（始终创建）
   └─ MatrixTargetBackend（仅桥接模式）
9. 创建 BridgeCore
10. 启动 WebServer（如果 web.enabled 且 message_store 激活）
11. 注册 SIGINT/SIGTERM 处理器
12. asyncio.run() — 启动桥接 + Web 服务器
13. 信号触发：取消桥接任务 → 停止后端 → 停止 Web → 刷新状态 → 关闭 DB → 退出
```

#### 交互式凭据设置

当后端段缺少 `access_token` 时，`setup_credentials()`：
1. 提示输入主密码（需确认）
2. 使用配置的密码登录
3. 用主密码加密收到的访问令牌
4. 将加密令牌写回 `config.yaml`
5. 提供可选的 E2EE 密钥文件导入

---

### 3.12 CLI 工具

#### `backfill.py` — 历史消息导入

连接到源 Matrix 服务器，批量导入历史房间消息到 MessageStore。

**功能：**
- 分页 `/messages` API 遍历（批量大小：250）
- 通过源机器人的加密存储支持 E2EE
- 媒体下载和本地存储
- 导入后编辑协调
- 撤回处理（删除被撤回的消息）
- 配置加密支持

**CLI 参数：**
```
--rooms       逗号分隔的房间 ID/别名（默认：所有已加入的）
--days N      导入最近 N 天（默认：30）
--limit N     最大导入消息总数
--no-media    跳过媒体下载
--dry-run     显示将要导入的内容
--log-level   设置日志级别
```

#### `repair_media.py` — 媒体修复工具

扫描本地保存的媒体文件以检测损坏（未解密就保存的密文），重新下载并解密。

**功能：**
- 对 25+ 种已知媒体格式签名的魔术字节检测
- 通过 `decrypt_attachment()` 重新下载 + 解密
- 原子文件替换
- 路径遍历保护
- `--dry-run` 模式

#### `encrypt_tool.py` — 配置加密工具

用于加密/解密单个配置值的交互式 CLI：

```
python encrypt_tool.py encrypt    # 加密一个值
python encrypt_tool.py decrypt    # 解密一个值
```

---

## 4. 数据流图

### 4.1 文本消息转发（A → B）

```
服务器 A                    Source Backend            BridgeCore              Target Backend              服务器 B
  │                              │                        │                        │                        │
  │── m.room.message ───────────►│                        │                        │                        │
  │   (Alice: "Hello")           │                        │                        │                        │
  │                              │── BridgeMessage ──────►│                        │                        │
  │                              │  FORWARD, TEXT         │                        │                        │
  │                              │                        │── 保存到存储 ─────────►│                        │
  │                              │                        │── 格式化文本 ─────────►│                        │
  │                              │                        │  "[#general] Alice:    │                        │
  │                              │                        │   Hello"               │── m.room.message ─────►│
  │                              │                        │                        │   (m.notice)           │
  │                              │                        │── 保存事件映射 ───────►│                        │
```

### 4.2 媒体转发（A → B）

```
服务器 A                    Source Backend            BridgeCore              Target Backend              服务器 B
  │                              │                        │                        │                        │
  │── m.room.message ───────────►│                        │                        │                        │
  │   (m.image, mxc://A/xxx)     │                        │                        │                        │
  │                              │── download(mxc://A/xxx)│                        │                        │
  │◄── 二进制数据 ──────────────│                        │                        │                        │
  │                              │── BridgeMessage ──────►│                        │                        │
  │                              │  FORWARD, IMAGE,       │                        │                        │
  │                              │  media_data=<bytes>    │── 保存到存储 ─────────►│                        │
  │                              │                        │── send_media() ───────►│                        │
  │                              │                        │                        │── upload(bytes) ───────►│
  │                              │                        │                        │◄── mxc://B/yyy ────────│
  │                              │                        │                        │── m.room.message ─────►│
  │                              │                        │                        │   (url: mxc://B/yyy)   │
  │                              │                        │                        │── m.notice (标题) ────►│
```

### 4.3 编辑转发（A → B）

```
服务器 A                    Source Backend            BridgeCore              Target Backend              服务器 B
  │                              │                        │                        │                        │
  │── m.room.message ───────────►│                        │                        │                        │
  │   (m.replace, new_content)   │                        │                        │                        │
  │                              │── BridgeMessage ──────►│                        │                        │
  │                              │  EDIT, edit_of=$orig   │                        │                        │
  │                              │                        │── 更新存储 ───────────►│                        │
  │                              │                        │── 查找目标事件 ───────►│                        │
  │                              │                        │                        │                        │
  │                              │                        │── edit_message() ─────►│── m.room.message ─────►│
  │                              │                        │  (m.replace)           │   (已编辑的消息)        │
```

### 4.4 撤回转发（A → B）

```
服务器 A                    Source Backend            BridgeCore              Target Backend              服务器 B
  │                              │                        │                        │                        │
  │── m.room.redaction ─────────►│                        │                        │                        │
  │                              │── BridgeMessage ──────►│                        │                        │
  │                              │  REDACT                │── 从存储删除 ─────────►│                        │
  │                              │                        │── 查找目标事件 ───────►│                        │
  │                              │                        │── redact_event() ─────►│── m.room.redaction ───►│
  │                              │                        │── 移除映射 ───────────►│                        │
```

### 4.5 回复命令（B → A）

```
服务器 B                    Target Backend            BridgeCore              Source Backend             服务器 A
  │                              │                        │                        │                        │
  │── m.room.message ───────────►│                        │                        │                        │
  │   "!send #general Hi"        │                        │                        │                        │
  │                              │── 解析命令 ───────────►│                        │                        │
  │                              │  REPLY, target=#general │                        │                        │
  │                              │                        │── resolve_room_id ────►│                        │
  │                              │                        │  "#general" → "!abc"    │                        │
  │                              │                        │                        │                        │
  │                              │                        │── send_message ────────►│── m.room.message ─────►│
  │                              │                        │  "[Bob via bridge] Hi"  │                        │
  │                              │                        │                        │                        │
  │                              │◄── send_reaction(✓) ───│                        │                        │
```

### 4.6 Reply-to（B → A）

```
服务器 B                    Target Backend            BridgeCore              Source Backend             服务器 A
  │                              │                        │                        │                        │
  │── m.room.message ───────────►│                        │                        │                        │
  │   (m.in_reply_to: $fwd_ev)   │                        │                        │                        │
  │                              │── REPLY ──────────────►│                        │                        │
  │                              │  reply_to=$fwd_ev      │                        │                        │
  │                              │                        │── 查找房间映射 ────────►│                        │
  │                              │                        │  $fwd_ev → room "!abc"  │                        │
  │                              │                        │── send_message ────────►│── m.room.message ─────►│
  │                              │                        │                        │                        │
```

### 4.7 控制命令（!login）

```
目标房间中的用户          Target Backend            BridgeCore              Source Backend             服务器 A
  │                              │                        │                        │                        │
  │── "!login" ─────────────────►│                        │                        │                        │
  │                              │── CONTROL ────────────►│                        │                        │
  │                              │  text="login"          │                        │                        │
  │                              │                        │── source.start() ─────►│── 连接 ──────────────►│
  │                              │                        │── 设置 forwarding=True   │                        │
  │                              │                        │── 持久化状态 ──────────►│                        │
  │                              │                        │                        │                        │
  │◄── "Source connected" ───────│◄── send_notice() ─────│                        │                        │
```

---

## 5. E2EE 详情

### 5.1 密钥生命周期

```
首次运行：
  1. AsyncClient 生成身份密钥（Ed25519 + Curve25519）
  2. 生成一次性密钥（OTK）
  3. 通过 /keys/upload 上传公钥到服务器
  4. 将所有密钥持久化到 store_path（SQLite）

后续运行：
  1. 从 store_path 加载现有密钥
  2. 如需要则上传新的 OTK（should_upload_keys）
  3. 查询其他用户的密钥（should_query_keys）

密钥导入（可选）：
  1. 加载密钥导出文件（例如从 Element 导出）
  2. 通过 client.import_keys() 导入 Megolm 会话密钥
  3. 从运行时配置中清除 key_import_file 和 key_import_passphrase
```

### 5.2 解密流程

```
加密事件（MegolmEvent）
    │
    ├─ OlmMachine 获取 Megolm 会话密钥
    │  （通过 to-device 从房间成员接收）
    │
    ├─ 解密负载
    │
    └─ 成功 ──► 重新分发为 RoomMessage
       │
       └─ 失败 ──► 排队等待重试
           ├─ 保存到 _pending_encrypted
           ├─ 请求房间密钥
           ├─ 查询 + 声明发送者的设备密钥
           ├─ 持久化到 StateManager 以供跨重启重试
           └─ 密钥到达时重试（内存中或持久化的）
```

### 5.3 E2EE 的关键要求

| 要求 | 原因 |
|------|------|
| `device_id` 不得更改 | 更改会创建新设备，丢失所有会话密钥 |
| `store_path` 必须持久化 | 包含 Olm/Megolm 会话密钥 — 删除不可逆 |
| 机器人必须在消息发送前加入房间 | Megolm 会话密钥在发送时分发 |
| 用户应验证机器人设备 | 防止客户端中出现"未验证设备"警告 |
| `key_import_file` 可引导解密 | 从另一个客户端导入密钥以解密历史消息 |

---

## 6. 循环防护与去重

### 四层防护：

| 层 | 机制 | 位置 |
|----|------|------|
| 1. 发送者检查 | `event.sender == self.config["user_id"]` + 设备检查 → 跳过 | 两个后端 |
| 2. 事件去重 | `state.is_processed(event_id)` → 跳过 | 两个后端 |
| 3. 方向过滤 | 仅在源端处理 FORWARD/EDIT/REDACT，在目标端处理 REPLY/CONTROL | BridgeCore |
| 4. 自身消息过滤 | `msg.from_self == True` → 跳过转发 | BridgeCore |

---

## 7. 扩展新协议

### 添加 Telegram 后端（示例）

**步骤 1：** 创建 `backends/telegram.py`

```python
class TelegramBackend(BaseBackend):
    async def start(self):
        # 连接到 Telegram Bot API
        # 轮询更新
        # 收到消息时：构建 BridgeMessage → self._emit_message()

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

**步骤 2：** 更新 `config.yaml`

**步骤 3：** 更新 `main.py`

无需更改 `BridgeCore`、`BridgeMessage` 或 `MessageStore`。

---

## 8. 配置 Schema

```yaml
logging:
  level: string                    # DEBUG | INFO | WARNING | ERROR
  file: string                     # 日志文件路径（空 = 仅 stdout）
  max_bytes: integer               # 日志轮转前的最大文件大小（默认：10MB）
  backup_count: integer            # 保留的轮转日志文件数量（默认：3）

source:                            # 连接到服务器 A 的后端
  type: string                     # "matrix"（未来："telegram" 等）
  homeserver: string               # 必需。例如 "https://matrix-a.example.com"
  user_id: string                  # 必需。完整 MXID
  access_token: string             # 访问令牌或密码（二选一）
  password: string                 # 仅首次运行使用
  device_id: string                # E2EE 必须固定
  store_path: string               # E2EE 加密存储目录
  handle_encrypted: boolean        # 默认：true
  media_max_size: integer          # 最大媒体下载大小（字节，默认：50MB）
  key_import_file: string          # E2EE 密钥导出文件路径（可选）
  key_import_passphrase: string    # 密钥导出文件的口令

target:                            # 连接到服务器 B 的后端（省略则为备份模式）
  type: string
  homeserver: string
  user_id: string
  access_token: string
  password: string
  device_id: string
  store_path: string
  handle_encrypted: boolean
  target_room: string              # 聚合房间的 Room ID
  key_import_file: string
  key_import_passphrase: string

bridge:
  command_prefix: string           # 默认："!send"
  message_format: string           # 包含 {room_name}, {sender}, {text} 的模板
  state_path: string               # 默认："state.json"
  admin_users: list[string]        # 授权发出控制命令的 MXID 列表（空 = 任何人）
  media:
    enabled: boolean               # 默认：true
  call_notifications:
    enabled: boolean               # 默认：true
  message_store:
    enabled: boolean               # 默认：false
    path: string                   # SQLite 数据库路径（默认："messages.db"）
    media_dir: string              # 媒体文件的本地目录（默认："./media"）
  web:
    enabled: boolean               # 默认：false（需要 message_store）
    host: string                   # 绑定主机（默认："0.0.0.0"）
    port: integer                  # 绑定端口（默认：8080）
    password: string               # 远程访问必需；空 = 仅限 localhost
    trusted_proxy: boolean         # 信任 X-Forwarded-For 请求头（默认：false）
```

---

## 9. 依赖项

| 包 | 版本 | 用途 |
|---|------|------|
| `matrix-nio[e2e]` | >= 0.24.0 | 支持 E2EE 的异步 Matrix 客户端 |
| `PyYAML` | >= 6.0 | 配置文件解析 |
| `cryptography` | >= 42.0 | 配置字段加密的 Fernet 加密 |
| `aiohttp` | >= 3.9.0 | Web 搜索界面的 HTTP 服务器 |
| `peewee` | >= 3.17.0 | 消息持久化的 SQLite ORM |

### E2EE 的 matrix-nio 子依赖

| 包 | 用途 |
|---|------|
| `python-olm` | Olm/Megolm 加密操作 |
| `pycryptodome` | 消息加密的 AES/HMAC |
| `atomicwrites` | 密钥存储的原子文件写入 |
| `cachetools` | 密钥缓存 |
| `unpaddedbase64` | Matrix 特定的 base64 编码 |

### 间接依赖（通过 matrix-nio）

| 包 | 用途 |
|---|------|
| `aiofiles` | 状态持久化的异步文件 I/O |
