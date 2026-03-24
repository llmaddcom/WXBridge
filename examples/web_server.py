"""
WXBridge 调试 Web Server

用途：快速验证 WXBridge 集成效果，提供可视化的登录、消息收发界面。

功能：
  - 微信扫码登录（支持 Redis / 内存两种存储）
  - 实时查看收发消息（SSE 推送）
  - 手动向用户发送文本 / 图片 / 文件 / 视频
  - Echo 模式（可切换）：自动将用户消息原样回显

依赖（在项目根目录执行）：
  pip install 'wxbridge[media]' fastapi uvicorn

启动（默认使用内存存储，无需 Redis）：
  python examples/web_server.py

使用 Redis 持久化 token（重启后无需重新扫码）：
  REDIS_URL=redis://localhost python examples/web_server.py

指定端口：
  PORT=8080 python examples/web_server.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from collections import deque
from typing import Any, AsyncGenerator

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from wxbridge import AIAdapter, Reply, WeixinBridge, WeixinMessage, configure_logging
from wxbridge.ilink_client import MEDIA_TYPE_MAP, ILinkClient, upload_media
from wxbridge.storage import DictStorage, RedisStorage

configure_logging()
logger = logging.getLogger(__name__)

MAX_MESSAGES = 500  # 内存中保留最近 N 条消息


# ──────────────────────────────────────────────────────────────────────────────
# 消息存储
# ──────────────────────────────────────────────────────────────────────────────

class MessageLog:
    """线程安全的消息日志（asyncio）"""

    def __init__(self) -> None:
        self._messages: deque[dict[str, Any]] = deque(maxlen=MAX_MESSAGES)
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        # 每个用户最新的 context_token（用于主动回复）
        self.user_context: dict[str, str] = {}

    def add(self, entry: dict[str, Any]) -> None:
        self._messages.append(entry)
        for q in self._subscribers:
            q.put_nowait(entry)

    def all(self) -> list[dict[str, Any]]:
        return list(self._messages)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# 适配器
# ──────────────────────────────────────────────────────────────────────────────

class WebServerAdapter(AIAdapter):
    """
    调试用适配器：
      - 记录所有入站消息到 MessageLog
      - echo_mode=True 时原样回显（文本回文本，图片/文件回同类）
      - 将媒体 bytes 转为 base64 存入日志，供前端展示缩略图
    """

    def __init__(self, log: MessageLog, echo_mode: bool = True) -> None:
        self._log = log
        self.echo_mode = echo_mode

    async def reply(self, message: WeixinMessage) -> str | Reply:
        # 缓存 context_token（主动发送时需要）
        self._log.user_context[message.from_user_id] = message.context_token

        # 构建日志条目
        items_log: list[dict[str, Any]] = []
        for item in message.items:
            e: dict[str, Any] = {"type": item.type}
            if item.text:
                e["text"] = item.text
            if item.media_bytes:
                e["media_b64"] = base64.b64encode(item.media_bytes).decode()
                e["size"] = len(item.media_bytes)
            if item.filename:
                e["filename"] = item.filename
            items_log.append(e)

        self._log.add({
            "id": str(uuid.uuid4()),
            "direction": "in",
            "from_user_id": message.from_user_id,
            "text": message.text,
            "items": items_log,
            "ts": message.create_time_ms,
        })

        if not self.echo_mode:
            return ""

        # 文本 echo
        if message.text:
            return f"[Echo] {message.text}"

        # 媒体 echo
        if message.media_items:
            item = message.media_items[0]
            if item.media_bytes:
                type_map = {2: "image", 4: "file", 5: "video"}
                mtype = type_map.get(item.type, "file")
                if mtype == "image":
                    return Reply.image(item.media_bytes)
                fname = item.filename or f"echo.{mtype}"
                if mtype == "video":
                    return Reply.video(item.media_bytes, fname)
                return Reply.file(item.media_bytes, fname)

        return ""


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI 应用工厂
# ──────────────────────────────────────────────────────────────────────────────

def create_app(
    redis_url: str | None = None,
    echo_mode: bool = True,
) -> FastAPI:
    """
    创建 FastAPI 应用。

    Args:
        redis_url: Redis URL；为 None 时使用内存存储（重启后需重新扫码）
        echo_mode: 是否自动回显用户消息
    """
    msg_log = MessageLog()
    adapter = WebServerAdapter(msg_log, echo_mode=echo_mode)

    storage = RedisStorage(redis_url) if redis_url else DictStorage()
    bridge = WeixinBridge(
        adapter=adapter,
        storage=storage,
        auto_download_media=True,
    )

    app = FastAPI(title="WXBridge 调试面板", docs_url=None, redoc_url=None)

    # ── 生命周期 ─────────────────────────────────────────────────────────────

    @app.on_event("startup")
    async def _startup() -> None:
        token_info = await bridge.auth.load_token()
        if token_info:
            await bridge.start()
            logger.info("检测到已有 token，bridge 已自动启动")
        else:
            logger.info("未找到 token，请访问 http://localhost:8000 扫码登录")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await bridge.stop()

    # ── 登录 ─────────────────────────────────────────────────────────────────

    @app.post("/api/login/start")
    async def login_start() -> dict[str, str]:
        """发起扫码登录，返回二维码图片与二维码 token"""
        token, img = await bridge.auth.start_login()
        asyncio.create_task(_poll_and_start(bridge), name="weixin_poll_login")
        img_b64 = img if isinstance(img, str) else base64.b64encode(img).decode()
        return {"status": "pending", "qrcode_img": img_b64, "qrcode_token": token}

    async def _poll_and_start(b: WeixinBridge) -> None:
        result = await b.auth.poll_login()
        if result == "confirmed":
            await b.start()
            logger.info("扫码登录成功，bridge 已启动")
        else:
            logger.warning("扫码登录失败：%s", result)

    @app.get("/api/login/status")
    async def login_status() -> dict[str, str]:
        status = await bridge.auth.get_login_status()
        return {"status": status}

    @app.get("/api/login/qrcode")
    async def login_qrcode() -> Response:
        """以 image/png 返回当前二维码图片"""
        img = await bridge.auth.get_pending_qrcode_img()
        if not img:
            raise HTTPException(404, "暂无二维码，请先 POST /api/login/start")
        img_bytes = base64.b64decode(img) if isinstance(img, str) else img
        return Response(content=img_bytes, media_type="image/png")

    @app.post("/api/logout")
    async def logout() -> dict[str, str]:
        await bridge.stop()
        await bridge.auth.clear_token()
        return {"status": "ok"}

    # ── Bridge 控制 ───────────────────────────────────────────────────────────

    @app.get("/api/bridge/status")
    async def bridge_status_ep() -> dict[str, Any]:
        return {
            "running": bridge.is_running,
            "healthy": await bridge.is_healthy(),
            "login_status": await bridge.auth.get_login_status(),
            "echo_mode": adapter.echo_mode,
            "storage": "redis" if redis_url else "memory",
        }

    @app.post("/api/bridge/start")
    async def bridge_start_ep() -> dict[str, str]:
        await bridge.start()
        return {"status": "started"}

    @app.post("/api/bridge/stop")
    async def bridge_stop_ep() -> dict[str, str]:
        await bridge.stop()
        return {"status": "stopped"}

    @app.post("/api/bridge/echo")
    async def toggle_echo(enabled: bool = Form(...)) -> dict[str, Any]:
        adapter.echo_mode = enabled
        return {"echo_mode": adapter.echo_mode}

    # ── 消息 ─────────────────────────────────────────────────────────────────

    @app.get("/api/messages")
    async def get_messages() -> list[dict[str, Any]]:
        """返回内存中所有消息记录"""
        return msg_log.all()

    @app.get("/api/messages/stream")
    async def messages_stream(request: Request) -> StreamingResponse:
        """SSE 实时推送消息（已有记录先发一次，之后增量推送）"""

        async def generate() -> AsyncGenerator[str, None]:
            # 推送历史消息
            for m in msg_log.all():
                yield f"data: {json.dumps(m, ensure_ascii=False)}\n\n"

            q = msg_log.subscribe()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        entry = await asyncio.wait_for(q.get(), timeout=20)
                        yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                msg_log.unsubscribe(q)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── 发送 ─────────────────────────────────────────────────────────────────

    async def _require_ilink(to_user_id: str) -> tuple[ILinkClient, str]:
        """加载 token + 校验 context_token，返回 (ilink_client, context_token)"""
        token_info = await bridge.auth.load_token()
        if not token_info:
            raise HTTPException(403, "尚未登录微信，请先扫码")
        bot_token, _, base_url = token_info
        ctx = msg_log.user_context.get(to_user_id)
        if not ctx:
            raise HTTPException(
                400,
                f"找不到用户 {to_user_id} 的 context_token，请等待该用户先发一条消息",
            )
        return ILinkClient(bot_token=bot_token, base_url=base_url), ctx

    @app.post("/api/send/text")
    async def send_text(
        to_user_id: str = Form(...),
        text: str = Form(...),
    ) -> dict[str, Any]:
        """向指定用户发送文本消息"""
        ilink, ctx = await _require_ilink(to_user_id)
        async with httpx.AsyncClient() as client:
            ok = await ilink.sendmessage(client, to_user_id, ctx, text)
        msg_log.add({
            "id": str(uuid.uuid4()),
            "direction": "out",
            "to_user_id": to_user_id,
            "text": text,
            "items": [{"type": 1, "text": text}],
            "ts": 0,
        })
        return {"ok": ok}

    @app.post("/api/send/media")
    async def send_media(
        to_user_id: str = Form(...),
        media_type: str = Form("file"),  # image | file | video
        file: UploadFile = File(...),
    ) -> dict[str, Any]:
        """向指定用户发送图片 / 文件 / 视频"""
        ilink, ctx = await _require_ilink(to_user_id)
        data = await file.read()
        media_type_int = MEDIA_TYPE_MAP.get(media_type, 4)

        async with httpx.AsyncClient() as client:
            item_dict = await upload_media(
                ilink, client, data, media_type_int,
                filename=file.filename or "", to_user_id=to_user_id,
            )
            ok = await ilink.sendmessage_items(client, to_user_id, ctx, [item_dict])

        msg_log.add({
            "id": str(uuid.uuid4()),
            "direction": "out",
            "to_user_id": to_user_id,
            "text": None,
            "items": [{"type": media_type_int, "filename": file.filename, "size": len(data)}],
            "ts": 0,
        })
        return {"ok": ok}

    # ── 用户列表 ─────────────────────────────────────────────────────────────

    @app.get("/api/users")
    async def list_users() -> list[str]:
        """返回曾发过消息的用户 ID 列表"""
        return list(msg_log.user_context.keys())

    # ── 前端 UI ───────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def ui() -> str:
        return _HTML

    return app


# ──────────────────────────────────────────────────────────────────────────────
# 内嵌 HTML 前端
# ──────────────────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WXBridge 调试面板</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #f0f2f5; color: #333; height: 100vh; display: flex; flex-direction: column; }
header { background: #07c160; color: #fff; padding: 12px 20px;
         display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
header h1 { font-size: 18px; }
.badge { font-size: 11px; background: rgba(255,255,255,.25); padding: 2px 8px; border-radius: 10px; }
.main { display: flex; flex: 1; overflow: hidden; }

/* ── 左侧面板 ── */
.sidebar { width: 280px; background: #fff; border-right: 1px solid #e8e8e8;
           display: flex; flex-direction: column; flex-shrink: 0; overflow-y: auto; }
.section { padding: 16px; border-bottom: 1px solid #f0f0f0; }
.section h3 { font-size: 13px; color: #888; text-transform: uppercase;
              letter-spacing: .5px; margin-bottom: 10px; }

/* 登录区 */
#qr-wrap { text-align: center; }
#qr-img { width: 160px; height: 160px; border: 1px solid #e8e8e8;
           border-radius: 8px; object-fit: contain; }
.status-dot { display: inline-block; width: 8px; height: 8px;
              border-radius: 50%; margin-right: 6px; }
.dot-ok { background: #07c160; }
.dot-warn { background: #faad14; }
.dot-err { background: #ff4d4f; }
.dot-off { background: #d9d9d9; }

/* 用户列表 */
.user-item { padding: 8px 10px; border-radius: 6px; cursor: pointer;
             font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.user-item:hover { background: #f5f5f5; }
.user-item.active { background: #e6f7ff; color: #1677ff; }

/* 按钮 */
btn, .btn { display: inline-block; padding: 7px 14px; border-radius: 6px; border: none;
            cursor: pointer; font-size: 13px; transition: opacity .15s; }
.btn:hover { opacity: .85; }
.btn-primary { background: #07c160; color: #fff; }
.btn-danger  { background: #ff4d4f; color: #fff; }
.btn-default { background: #f0f0f0; color: #555; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.btn-full { width: 100%; margin-top: 6px; }
.row { display: flex; gap: 8px; }

/* ── 右侧对话区 ── */
.chat-wrap { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.chat-header { padding: 12px 20px; background: #fff; border-bottom: 1px solid #e8e8e8;
               font-size: 14px; font-weight: 600; flex-shrink: 0; display: flex; align-items: center; gap: 8px; }
.chat-log { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px; }

/* 消息气泡 */
.msg { max-width: 70%; }
.msg.in  { align-self: flex-start; }
.msg.out { align-self: flex-end; }
.bubble { padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.5; word-break: break-word; }
.msg.in  .bubble { background: #fff; border: 1px solid #e8e8e8; border-top-left-radius: 2px; }
.msg.out .bubble { background: #07c160; color: #fff; border-top-right-radius: 2px; }
.msg-meta { font-size: 11px; color: #aaa; margin-top: 3px; }
.msg.out .msg-meta { text-align: right; }
.msg-img { max-width: 200px; max-height: 200px; border-radius: 8px; cursor: pointer; display: block; margin-top: 4px; }
.msg-file { display: flex; align-items: center; gap: 8px; background: rgba(0,0,0,.06);
            padding: 8px 12px; border-radius: 8px; font-size: 13px; }
.file-icon { font-size: 22px; }

/* 发送区 */
.send-wrap { background: #fff; border-top: 1px solid #e8e8e8; padding: 14px 20px; flex-shrink: 0; }
.send-wrap textarea { width: 100%; border: 1px solid #d9d9d9; border-radius: 8px;
                      padding: 10px; font-size: 14px; resize: none; outline: none;
                      font-family: inherit; }
.send-wrap textarea:focus { border-color: #07c160; }
.send-actions { display: flex; gap: 8px; margin-top: 8px; align-items: center; }
.send-actions select { padding: 6px 10px; border: 1px solid #d9d9d9; border-radius: 6px;
                        font-size: 13px; outline: none; }
#file-input { display: none; }
.file-label { padding: 6px 12px; border: 1px dashed #d9d9d9; border-radius: 6px;
              font-size: 13px; cursor: pointer; color: #666; white-space: nowrap; }
.file-label:hover { border-color: #07c160; color: #07c160; }
.spacer { flex: 1; }

/* echo 开关 */
.toggle-wrap { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.toggle { position: relative; display: inline-block; width: 36px; height: 20px; }
.toggle input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; inset: 0; background: #ccc; border-radius: 10px; cursor: pointer; transition: .2s; }
.slider:before { content: ""; position: absolute; width: 14px; height: 14px;
                  left: 3px; bottom: 3px; background: #fff; border-radius: 50%; transition: .2s; }
input:checked + .slider { background: #07c160; }
input:checked + .slider:before { transform: translateX(16px); }

/* 无对话提示 */
.empty-hint { flex: 1; display: flex; align-items: center; justify-content: center;
              color: #aaa; font-size: 14px; }
</style>
</head>
<body>

<header>
  <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
    <path d="M20 2H4C2.9 2 2 2.9 2 4v16l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z" fill="white"/>
  </svg>
  <h1>WXBridge 调试面板</h1>
  <span class="badge" id="storage-badge">storage: ...</span>
  <span style="flex:1"></span>
  <span id="bridge-badge" class="badge">● 未启动</span>
</header>

<div class="main">

  <!-- ── 左侧 ── -->
  <div class="sidebar">

    <!-- 登录 -->
    <div class="section">
      <h3>微信登录</h3>
      <div id="qr-wrap" style="display:none">
        <p style="margin-top:8px;font-size:12px;color:#888">请点击链接并扫码登录</p>
        <p id="qr-link-wrap" style="margin-top:6px;font-size:12px;display:none">
          原链接：<a id="qr-link" href="#" target="_blank" rel="noopener noreferrer"></a>
        </p>
      </div>
      <div id="login-status" style="font-size:13px;margin-bottom:10px">
        <span class="status-dot dot-off"></span>检查中...
      </div>
      <div class="row">
        <button class="btn btn-primary" style="flex:1" onclick="startLogin()">扫码登录</button>
        <button class="btn btn-default" style="flex:1" onclick="doLogout()">退出登录</button>
      </div>
    </div>

    <!-- Bridge -->
    <div class="section">
      <h3>Bridge 控制</h3>
      <div class="toggle-wrap" style="margin-bottom:10px">
        <label class="toggle">
          <input type="checkbox" id="echo-toggle" checked onchange="toggleEcho(this.checked)">
          <span class="slider"></span>
        </label>
        Echo 模式（自动回显）
      </div>
      <div class="row">
        <button class="btn btn-primary btn-sm" style="flex:1" onclick="startBridge()">启动</button>
        <button class="btn btn-danger btn-sm" style="flex:1" onclick="stopBridge()">停止</button>
      </div>
    </div>

    <!-- 用户列表 -->
    <div class="section" style="flex:1">
      <h3>用户列表</h3>
      <div id="user-list" style="display:flex;flex-direction:column;gap:4px">
        <div style="font-size:12px;color:#ccc">等待用户发消息...</div>
      </div>
    </div>

  </div>

  <!-- ── 右侧 ── -->
  <div class="chat-wrap">
    <div class="chat-header">
      <span id="chat-title">选择用户开始对话</span>
    </div>

    <div id="chat-log" class="chat-log">
      <div class="empty-hint">← 从左侧选择用户查看对话</div>
    </div>

    <div class="send-wrap">
      <textarea id="send-text" rows="3" placeholder="输入消息... (Ctrl+Enter 发送)"></textarea>
      <div class="send-actions">
        <label class="file-label" for="file-input">📎 选择文件</label>
        <input type="file" id="file-input" onchange="onFileSelected(this)">
        <span id="file-name" style="font-size:12px;color:#888;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
        <select id="media-type">
          <option value="file">文件</option>
          <option value="image">图片</option>
          <option value="video">视频</option>
        </select>
        <span class="spacer"></span>
        <button class="btn btn-primary" onclick="sendMessage()">发送</button>
      </div>
    </div>
  </div>

</div>

<script>
// ── 状态 ──────────────────────────────────────────────────────────────────────
let activeUser = null;
const allMessages = [];    // {id, direction, from_user_id/to_user_id, text, items, ts}
const userMessages = {};   // userId → [msg, ...]

// ── SSE ───────────────────────────────────────────────────────────────────────
function startSSE() {
  const es = new EventSource('/api/messages/stream');
  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (allMessages.find(m => m.id === msg.id)) return;  // 去重
    allMessages.push(msg);
    const uid = msg.from_user_id || msg.to_user_id;
    if (uid) {
      if (!userMessages[uid]) userMessages[uid] = [];
      userMessages[uid].push(msg);
    }
    refreshUserList();
    if (uid === activeUser) appendMessage(msg);
  };
  es.onerror = () => setTimeout(startSSE, 3000);
}

// ── 状态轮询 ─────────────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const r = await fetch('/api/bridge/status').then(r => r.json());
    const badge = document.getElementById('bridge-badge');
    const loginEl = document.getElementById('login-status');

    document.getElementById('storage-badge').textContent = 'storage: ' + (r.storage || 'memory');

    if (r.healthy) {
      badge.textContent = '● 运行中';
      badge.style.background = 'rgba(7,193,96,.4)';
    } else if (r.running) {
      badge.textContent = '◌ 启动中';
      badge.style.background = 'rgba(250,173,20,.4)';
    } else {
      badge.textContent = '○ 已停止';
      badge.style.background = 'rgba(255,255,255,.2)';
    }

    document.getElementById('echo-toggle').checked = r.echo_mode;

    const ls = r.login_status;
    const dotCls = ls === 'confirmed' ? 'dot-ok' : ls === 'pending' ? 'dot-warn' : 'dot-err';
    const labelMap = { confirmed: '已登录', pending: '等待扫码', failed: '登录失败', none: '未登录' };
    loginEl.innerHTML = `<span class="status-dot ${dotCls}"></span>${labelMap[ls] || ls}`;

    if (ls === 'confirmed') {
      document.getElementById('qr-wrap').style.display = 'none';
    }
  } catch (_) {}
}

// ── 登录 ─────────────────────────────────────────────────────────────────────
async function startLogin() {
  const r = await fetch('/api/login/start', {method:'POST'}).then(r => r.json());
  const qr = (r.qrcode_img || '').trim();
  const linkWrap = document.getElementById('qr-link-wrap');
  const linkEl = document.getElementById('qr-link');
  if (qr.startsWith('http://') || qr.startsWith('https://')) {
    linkEl.href = qr;
    linkEl.textContent = qr;
    linkWrap.style.display = 'block';
  } else if (r.qrcode_token) {
    // 二维码图片不是 URL 时，展示 token 作为兜底原始信息
    linkEl.removeAttribute('href');
    linkEl.textContent = r.qrcode_token;
    linkWrap.style.display = 'block';
  } else {
    linkWrap.style.display = 'none';
  }

  document.getElementById('qr-wrap').style.display = 'block';
  // 轮询直到 confirmed
  const t = setInterval(async () => {
    const s = await fetch('/api/login/status').then(r => r.json());
    if (s.status === 'confirmed' || s.status === 'failed') {
      clearInterval(t);
      document.getElementById('qr-wrap').style.display = 'none';
      linkWrap.style.display = 'none';
      pollStatus();
    }
  }, 2000);
}

async function doLogout() {
  if (!confirm('确认退出登录？')) return;
  await fetch('/api/logout', {method:'POST'});
  pollStatus();
}

// ── Bridge ───────────────────────────────────────────────────────────────────
async function startBridge() {
  await fetch('/api/bridge/start', {method:'POST'});
  pollStatus();
}
async function stopBridge() {
  await fetch('/api/bridge/stop', {method:'POST'});
  pollStatus();
}
async function toggleEcho(v) {
  const fd = new FormData(); fd.append('enabled', v);
  await fetch('/api/bridge/echo', {method:'POST', body: fd});
}

// ── 用户列表 ─────────────────────────────────────────────────────────────────
function refreshUserList() {
  const uids = Object.keys(userMessages);
  const el = document.getElementById('user-list');
  if (!uids.length) return;
  el.innerHTML = uids.map(uid => {
    const msgs = userMessages[uid];
    const last = msgs[msgs.length - 1];
    const preview = last.text ? last.text.slice(0, 20) : '[媒体]';
    return `<div class="user-item ${uid===activeUser?'active':''}" onclick="selectUser('${uid}')">
      <div style="font-weight:600;font-size:13px">${uid.slice(-8)}</div>
      <div style="font-size:11px;color:#888;margin-top:2px">${escHtml(preview)}</div>
    </div>`;
  }).join('');
}

function selectUser(uid) {
  activeUser = uid;
  document.getElementById('chat-title').textContent = uid;
  refreshUserList();
  renderChat(uid);
}

// ── 对话渲染 ─────────────────────────────────────────────────────────────────
function renderChat(uid) {
  const log = document.getElementById('chat-log');
  log.innerHTML = '';
  (userMessages[uid] || []).forEach(msg => appendMessage(msg, false));
  log.scrollTop = log.scrollHeight;
}

function appendMessage(msg, scroll=true) {
  if (!activeUser) return;
  const uid = msg.from_user_id || msg.to_user_id;
  if (uid !== activeUser) return;

  const log = document.getElementById('chat-log');
  const div = document.createElement('div');
  div.className = 'msg ' + msg.direction;

  const ts = msg.ts ? new Date(msg.ts).toLocaleTimeString() : '';
  const meta = msg.direction === 'in'
    ? `${uid.slice(-8)} · ${ts}`
    : `我 · ${ts}`;

  let content = '';
  if (msg.items && msg.items.length) {
    for (const item of msg.items) {
      if (item.text) {
        content += `<div class="bubble">${escHtml(item.text)}</div>`;
      } else if (item.media_b64) {
        if (item.type === 2) {
          content += `<img class="msg-img" src="data:image/jpeg;base64,${item.media_b64}"
            onclick="window.open(this.src)" title="点击查看原图">`;
        } else {
          const fname = item.filename || '文件';
          const size = item.size ? ` (${(item.size/1024).toFixed(1)} KB)` : '';
          content += `<div class="bubble">
            <div class="msg-file">
              <span class="file-icon">${item.type===5?'🎬':'📄'}</span>
              <div><div style="font-weight:600">${escHtml(fname)}</div><div style="font-size:11px;color:#aaa">${size}</div></div>
            </div></div>`;
        }
      } else if (item.filename || item.size) {
        const fname = item.filename || '文件';
        const size = item.size ? ` (${(item.size/1024).toFixed(1)} KB)` : '';
        content += `<div class="bubble">
          <div class="msg-file">
            <span class="file-icon">📄</span>
            <div><div style="font-weight:600">${escHtml(fname)}</div><div style="font-size:11px;color:#aaa">${size}</div></div>
          </div></div>`;
      }
    }
  } else if (msg.text) {
    content = `<div class="bubble">${escHtml(msg.text)}</div>`;
  }

  div.innerHTML = content + `<div class="msg-meta">${meta}</div>`;
  log.appendChild(div);
  if (scroll) log.scrollTop = log.scrollHeight;
}

// ── 发送 ─────────────────────────────────────────────────────────────────────
let selectedFile = null;

function onFileSelected(input) {
  selectedFile = input.files[0] || null;
  document.getElementById('file-name').textContent = selectedFile ? selectedFile.name : '';
  // 根据扩展名自动选择类型
  if (selectedFile) {
    const ext = selectedFile.name.split('.').pop().toLowerCase();
    const imgExts = ['jpg','jpeg','png','gif','webp','bmp'];
    const vidExts = ['mp4','mov','avi','mkv','webm'];
    const sel = document.getElementById('media-type');
    if (imgExts.includes(ext)) sel.value = 'image';
    else if (vidExts.includes(ext)) sel.value = 'video';
    else sel.value = 'file';
  }
}

async function sendMessage() {
  if (!activeUser) { alert('请先选择用户'); return; }
  const text = document.getElementById('send-text').value.trim();

  if (selectedFile) {
    const fd = new FormData();
    fd.append('to_user_id', activeUser);
    fd.append('media_type', document.getElementById('media-type').value);
    fd.append('file', selectedFile);
    try {
      const r = await fetch('/api/send/media', {method:'POST', body:fd}).then(r=>r.json());
      if (!r.ok) alert('发送失败（iLink 返回非成功）');
    } catch(e) { alert('发送出错: ' + e); }
    selectedFile = null;
    document.getElementById('file-input').value = '';
    document.getElementById('file-name').textContent = '';
  } else if (text) {
    const fd = new FormData();
    fd.append('to_user_id', activeUser);
    fd.append('text', text);
    try {
      const r = await fetch('/api/send/text', {method:'POST', body:fd}).then(r=>r.json());
      if (!r.ok) alert('发送失败');
      document.getElementById('send-text').value = '';
    } catch(e) { alert('发送出错: ' + e.message); }
  }
}

document.getElementById('send-text').addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); sendMessage(); }
});

// ── 工具 ─────────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── 初始化 ───────────────────────────────────────────────────────────────────
startSSE();
pollStatus();
setInterval(pollStatus, 5000);
</script>
</body>
</html>
"""


# ──────────────────────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    redis_url = os.getenv("REDIS_URL")  # 不设则使用内存存储
    echo_mode = os.getenv("ECHO_MODE", "1").lower() not in ("0", "false", "no")
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    app = create_app(redis_url=redis_url, echo_mode=echo_mode)
    uvicorn.run(app, host=host, port=port, log_level="info")
