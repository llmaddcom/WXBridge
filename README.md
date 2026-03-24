# WXBridge

让**任何 AI 产品**都可以通过腾讯 iLink Bot API 接入微信个人号——而不仅仅是特定平台。

WXBridge 负责全部微信协议细节（认证、长轮询、游标持久化、消息解析、媒体加密传输、发送），你只需实现一个方法：

```
微信用户
    ↓ 发送消息（文本 / 图片 / 文件 / 视频）
腾讯 iLink Bot API  (长轮询，服务端 35s 超时)
    ↓
WeixinBridge  (核心循环 + 媒体下载/上传)
    ↓ 调用
AIAdapter  ←──── 你只需实现此接口
    ↓ 返回 str 或 Reply（文本 + 媒体任意组合）
sendmessage → 微信用户
```

---

## 安装

```bash
# 核心库（仅文本收发）
pip install wxbridge

# 含 Redis 支持（生产推荐）
pip install "wxbridge[redis]"

# 含媒体传输支持（图片/文件/视频，需要 cryptography）
pip install "wxbridge[media]"

# 全部功能（Redis + 媒体）
pip install "wxbridge[full]"
```

> 需要 Python 3.10+

---

## 快速开始

### 文本适配器（最简）

```python
from wxbridge import AIAdapter, WeixinMessage

class MyAdapter(AIAdapter):
    async def reply(self, message: WeixinMessage) -> str:
        # message.text          — 用户文本（或语音转写结果）
        # message.from_user_id  — 稳定的 iLink UID（用作用户唯一标识）
        return await my_ai.chat(message.from_user_id, message.text)
```

### 启动桥接

```python
import asyncio
from wxbridge import WeixinBridge

async def main():
    bridge = WeixinBridge(adapter=MyAdapter(), redis_url="redis://localhost")

    # 首次使用需要扫码登录
    if not await bridge.auth.load_token():
        qrcode_token, qrcode_img = await bridge.auth.start_login()
        print("请扫描二维码")
        await bridge.auth.poll_login()  # 等待用户扫码确认

    await bridge.start()
    await asyncio.Event().wait()  # 保持运行

asyncio.run(main())
```

---

## 媒体收发

> 需要 `pip install "wxbridge[media]"`（依赖 `cryptography`）。

媒体文件经 **AES-128-ECB 加密**后通过腾讯 CDN 传输。WXBridge 封装了全部加密/解密细节。

### 接收媒体（入站）

开启 `auto_download_media=True` 后，bridge 在调用 `adapter.reply()` 前自动下载并解密媒体内容，填充到 `item.media_bytes`：

```python
from wxbridge import AIAdapter, WeixinMessage, Reply
from wxbridge.models import AdapterReply

class MediaAdapter(AIAdapter):
    async def reply(self, message: WeixinMessage) -> AdapterReply:
        for item in message.media_items:   # 图片/文件/视频条目
            if item.media_bytes is None:
                return Reply.text("媒体下载失败")

            if item.type == 2:   # 图片
                # item.media_bytes 是解密后的原始图片字节
                return Reply.image(item.media_bytes)   # 直接回传

            elif item.type == 4:  # 文件
                return Reply.file(item.media_bytes, filename=item.filename or "file")

        return Reply.text(f"你说：{message.text}")

bridge = WeixinBridge(
    adapter=MediaAdapter(),
    redis_url="redis://localhost",
    auto_download_media=True,    # 开启自动下载
)
```

`message.media_items` 只返回图片（type=2）、文件（type=4）、视频（type=5）条目；
语音（type=3）已在服务端完成 STT 转写，直接通过 `message.text` 读取。

### 发送媒体（出站）

返回 `Reply` 对象即可，WXBridge 自动处理加密上传：

```python
from wxbridge.models import Reply

# 纯图片
return Reply.image(png_bytes)

# 纯文件
return Reply.file(pdf_bytes, filename="report.pdf")

# 视频
return Reply.video(mp4_bytes, filename="clip.mp4")

# 文本 + 图片（顺序发送两条消息）
return Reply(items=[
    TextReplyItem("这是结果图："),
    MediaReplyItem(data=chart_bytes, media_type="image"),
])

# 仍然可以直接返回 str（向后兼容）
return "你好！"
```

### `Reply` 类型参考

| 类型 | 说明 |
|---|---|
| `str` | 纯文本回复（向后兼容，等同于 `Reply.text(...)` ） |
| `Reply.text(content)` | 纯文本 |
| `Reply.image(data)` | 图片（bytes） |
| `Reply.file(data, filename)` | 文件（bytes + 文件名） |
| `Reply.video(data, filename?)` | 视频（bytes） |
| `Reply(items=[...])` | 任意组合，按顺序逐条发送 |

`AdapterReply = str | Reply`，二者均可作为 `reply()` 的返回值。

---

## AIAdapter 接口

```python
from wxbridge import AIAdapter, WeixinMessage
from wxbridge.models import AdapterReply

class AIAdapter(ABC):
    @abstractmethod
    async def reply(self, message: WeixinMessage) -> AdapterReply:
        """
        处理一条微信消息并返回回复。

        message 字段：
          message.text           str | None  — 用户文本或语音 STT 转写
          message.from_user_id   str         — 稳定的 iLink UID（用作用户唯一标识）
          message.session_id     str         — iLink 会话 ID
          message.context_token  str         — 由桥接层回传，适配器无需关心
          message.media_items    list        — 媒体条目（auto_download_media=True 时含 media_bytes）
          message.message_id     str         — 消息唯一 ID
          message.create_time_ms int         — 消息时间戳（毫秒）
        """
        ...

    async def on_new_session(self, from_user_id: str) -> None:
        """会话超时后首条消息触发（默认 no-op，可覆写以重置对话历史）"""
```

**支持的入站消息类型：**

| `item.type` | 含义 | 获取内容 |
|---|---|---|
| 1 | 文字消息 | `message.text` |
| 3 | 语音消息 | `message.text`（服务端 STT 转写） |
| 2 | 图片 | `item.media_bytes`（需 `auto_download_media=True`） |
| 4 | 文件 | `item.media_bytes` + `item.filename` + `item.filesize` |
| 5 | 视频 | `item.media_bytes` |

---

## WeixinBridge 参数

```python
WeixinBridge(
    adapter,                      # AI 适配器实例
    storage=None,                 # 存储后端，None 时自动使用 RedisStorage
    redis_url="redis://localhost", # Redis URL（storage=None 时有效）
    session_ttl=3600,             # 会话空闲 TTL（秒），超时后触发 on_new_session
    max_concurrent_tasks=10,      # 最大并发消息处理数
    auto_download_media=False,    # True=自动下载入站媒体到 item.media_bytes（需 cryptography）
)
```

---

## 工作原理

### 长轮询消息接收

WXBridge 通过 `/ilink/bot/getupdates` 长轮询（服务端最长 35 秒返回）。每次收到消息后：

1. 持久化新游标到 Redis（服务重启后从断点继续，不丢消息）
2. 过滤掉 Bot 自身发出的消息（`message_type=2`）
3. 对每条用户消息创建独立的 `asyncio.Task`，并发调用 `adapter.reply()`

### 媒体传输流程

**下载（入站）**：`encrypt_query_param` + `aes_key` → GET CDN → AES-128-ECB 解密 → `item.media_bytes`

**上传（出站）**：`MediaReplyItem.data` → 生成随机 AES 密钥 → 加密 → `getuploadurl` 申请 URL → PUT CDN → `sendmessage` 携带 CDN 引用

### errcode=-14 处理

iLink 返回 `errcode=-14` 表示 token 已过期。WXBridge 会：
- 自动清除存储中的 token
- 停止桥接循环
- 需调用 `bridge.auth.start_login()` 重新扫码登录

---

## 登录流程

微信登录通过扫描二维码完成，token 持久化到 Redis（重启后自动恢复，无需重新扫码）。

```python
# 检查是否已登录
if await bridge.auth.load_token():
    print("已登录，直接启动")
else:
    # 申请二维码
    qrcode_token, qrcode_img = await bridge.auth.start_login()
    # qrcode_img 是二维码图片数据，展示给用户扫描

    # 等待扫码确认（阻塞，自动处理过期重试）
    status = await bridge.auth.poll_login()
    # status: "confirmed" | "expired" | "error"

# 查询当前登录状态
status = await bridge.auth.get_login_status()
# "pending" | "confirmed" | "failed" | "none"

# 退出登录
await bridge.auth.clear_token()
```

---

## 存储后端

### RedisStorage（生产默认）

```python
bridge = WeixinBridge(adapter=MyAdapter(), redis_url="redis://localhost:6379")
```

### DictStorage（测试/开发）

```python
from wxbridge import DictStorage
bridge = WeixinBridge(adapter=MyAdapter(), storage=DictStorage())
```

### 自定义存储后端

实现 `Storage` Protocol：

```python
class MyStorage:
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int | None = None) -> None: ...
    async def delete(self, *keys: str) -> None: ...
    async def expire(self, key: str, ttl: int) -> None: ...
```

### Redis Key 规范

| Key | 内容 | TTL |
|---|---|---|
| `weixin:bot_token` | iLink bot token | 永久 |
| `weixin:bot_id` | iLink bot ID | 永久 |
| `weixin:base_url` | 账号专属 API base URL | 永久 |
| `weixin:cursor` | getupdates 游标 | 永久 |
| `weixin:login:qrcode_token` | 当前二维码 token | 5 分钟 |
| `weixin:login:qrcode_img` | 当前二维码图片 | 5 分钟 |
| `weixin:login:status` | 登录状态 | 15 分钟 |

---

## 嵌入 Web 框架

### FastAPI

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from wxbridge import WeixinBridge

bridge = WeixinBridge(adapter=MyAdapter(), auto_download_media=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bridge.start()
    yield
    await bridge.stop()

app = FastAPI(lifespan=lifespan)

@app.post("/weixin/login/start")
async def login_start():
    token, img = await bridge.auth.start_login()
    return {"qrcode_token": token, "qrcode_img": img}

@app.get("/weixin/login/status")
async def login_status():
    return {"status": await bridge.auth.get_login_status()}

@app.get("/weixin/health")
async def health():
    return {"healthy": await bridge.is_healthy()}
```

---

## 示例

| 文件 | 说明 |
|---|---|
| [`examples/echo_adapter.py`](examples/echo_adapter.py) | 最简 echo 适配器，用于调试和验证接入 |
| [`examples/openai_adapter.py`](examples/openai_adapter.py) | OpenAI ChatCompletion 适配器，支持多轮对话历史 |
| [`examples/claude_adapter.py`](examples/claude_adapter.py) | Claude API 适配器，支持多轮对话历史 |
| [`examples/media_adapter.py`](examples/media_adapter.py) | 媒体 echo 适配器，演示图片/文件接收与回传 |

---

## 开发

```bash
# 安装（含全部可选依赖）
pip install -e ".[dev]"

# 运行全部测试
pytest

# 运行单个测试
pytest tests/test_bridge.py::test_message_dispatch

# 代码检查
ruff check .
ruff format .

# 类型检查
mypy wxbridge/
```

---

## 许可证

MIT License
