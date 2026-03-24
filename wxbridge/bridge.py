"""
微信 iLink Bot 消息桥接服务

职责：维护长轮询循环，将微信用户消息路由至 AIAdapter，再把回复通过 sendmessage 发回微信。

工作流程：
  1. 加载存储中的 bot_token（未登录时等待）
  2. 循环调用 getupdates（长轮询）
  3. 收到用户消息（message_type=1）→ asyncio.create_task(_handle_message)
  4. _handle_message：
       - 若 auto_download_media=True，先下载媒体到 item.media_bytes
       - 调用 adapter.reply()
       - 按序发送每个 ReplyItem（文本/媒体）

异常处理：
  - errcode=-14（session 过期）：清除 token，停止 bridge，等待重新登录
  - 连续 3 次失败：退避 30s 再重试
  - 单条消息处理异常：记录日志，不影响主循环
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .adapter import AIAdapter
from .auth import WeixinAuth
from .ilink_client import MEDIA_TYPE_MAP, ILinkClient, ILinkHTTPError, upload_media
from .models import AdapterReply, MediaReplyItem, Reply, TextReplyItem, WeixinMessage
from .storage import WEIXIN_CURSOR, WEIXIN_SESSION_PREFIX, RedisStorage, Storage

logger = logging.getLogger(__name__)


def _normalize_reply(raw: AdapterReply) -> Reply:
    """将适配器返回值统一为 Reply 对象"""
    if isinstance(raw, str):
        return Reply(items=[TextReplyItem(raw)] if raw else [])
    return raw


class WeixinBridge:
    """
    微信 iLink Bot 消息桥接

    每条用户消息作为独立 asyncio.Task 并发处理，互不阻塞。

    Usage:
        bridge = WeixinBridge(adapter=MyAdapter(), redis_url="redis://localhost")
        await bridge.start()

    媒体支持（需安装 cryptography）：
        bridge = WeixinBridge(
            adapter=MyMediaAdapter(),
            redis_url="redis://localhost",
            auto_download_media=True,   # 自动下载入站媒体到 item.media_bytes
        )
    """

    def __init__(
        self,
        adapter: AIAdapter,
        storage: Storage | None = None,
        redis_url: str = "redis://localhost",
        session_ttl: int = 3600,
        max_concurrent_tasks: int = 10,
        auto_download_media: bool = False,
    ) -> None:
        """
        Args:
            adapter:              AI 适配器实例（实现了 reply() 方法）
            storage:              存储后端；None 时自动创建 RedisStorage(redis_url)
            redis_url:            Redis 连接 URL（storage=None 时有效）
            session_ttl:          会话空闲 TTL（秒），超时后下一条消息开新会话（默认 1 小时）
            max_concurrent_tasks: 最大并发消息处理数（默认 10）
            auto_download_media:  True 时在调用 adapter 前自动下载媒体到 item.media_bytes
                                  需要安装 cryptography：pip install 'wxbridge[media]'
        """
        self._adapter = adapter
        self._storage: Storage = storage if storage is not None else RedisStorage(redis_url)
        self._session_ttl = session_ttl
        self._auth = WeixinAuth(self._storage)
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._pending_tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._auto_download_media = auto_download_media

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def auth(self) -> WeixinAuth:
        """暴露认证管理器，供调用方发起登录流程"""
        return self._auth

    async def is_healthy(self) -> bool:
        """bridge 运行中且 token 有效时返回 True，用于 FastAPI 健康检查"""
        if not self.is_running:
            return False
        return await self._auth.load_token() is not None

    async def start(self) -> None:
        """
        启动消息桥接后台任务（幂等：已运行则跳过）

        调用前应确保 bot_token 已写入存储（即已完成微信登录）。
        """
        if self.is_running:
            logger.info("WeChat bridge 已在运行，跳过重复启动")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="weixin_bridge")
        logger.info("WeChat bridge 后台任务已启动")

    async def stop(self) -> None:
        """优雅停止桥接（等待进行中的消息处理完成，再取消主循环）"""
        self._running = False
        # 等待所有进行中的消息处理任务完成
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("WeChat bridge 已停止")

    # ----------------------------------------------------------------
    # 主循环
    # ----------------------------------------------------------------

    async def _run(self) -> None:
        """
        长轮询主循环

        每次循环：
          1. 从存储读取 bot_token（若无则等待 10s）
          2. 调用 getupdates（最长阻塞 35s）
          3. 对每条用户消息派发独立 Task（复用 HTTP 连接池）
          4. 异常时指数退避，errcode=-14 时清除 token 并停止
        """
        logger.info("WeChat bridge 主循环启动")
        consecutive_errors = 0
        current_token: str | None = None
        ilink: ILinkClient | None = None

        async with self._storage:
            async with httpx.AsyncClient() as http_client:
                while self._running:
                    # 加载 token（每轮都重新加载，支持 token 热刷新）
                    token_info = await self._auth.load_token()
                    if not token_info:
                        logger.warning(
                            "WeChat bridge: 尚未登录微信，10s 后重试（请先调用 auth.start_login()）"
                        )
                        await asyncio.sleep(10)
                        continue

                    bot_token, _bot_id, base_url = token_info
                    # token 未变时复用 ILinkClient，避免重复构造
                    if bot_token != current_token:
                        ilink = ILinkClient(bot_token=bot_token, base_url=base_url)
                        current_token = bot_token

                    cursor = await self._storage.get(WEIXIN_CURSOR) or ""

                    try:
                        msgs, new_cursor, errcode = await ilink.getupdates(http_client, cursor)  # type: ignore[union-attr]
                    except asyncio.CancelledError:
                        raise
                    except ILinkHTTPError as e:
                        if e.auth_failed:
                            logger.error("WeChat HTTP 401: token 失效，清除 token 并停止 bridge")
                            await self._auth.clear_token()
                            self._running = False
                            return
                        consecutive_errors += 1
                        backoff = 60 if e.rate_limited else (30 if consecutive_errors >= 3 else 2)
                        logger.warning(
                            "WeChat HTTP %d（第 %d 次）: %s，%ds 后重试",
                            e.status_code, consecutive_errors, e, backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    except Exception as e:
                        consecutive_errors += 1
                        backoff = 30 if consecutive_errors >= 3 else 2
                        logger.warning(
                            "WeChat getupdates 异常（第 %d 次）: %s，%ds 后重试",
                            consecutive_errors, e, backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue

                    consecutive_errors = 0

                    # session 过期（errcode=-14）：token 已失效，清除并停止 bridge
                    if errcode == -14:
                        logger.error(
                            "WeChat session 已过期（errcode=-14），token 已失效。"
                            "正在清除 token，请重新调用 auth.start_login() 扫码登录"
                        )
                        await self._auth.clear_token()
                        self._running = False
                        return

                    # 持久化游标（服务重启后从断点继续）
                    if new_cursor and new_cursor != cursor:
                        await self._storage.set(WEIXIN_CURSOR, new_cursor)

                    # 只处理用户发来的消息（message_type=1），跳过 Bot 自己发出的（message_type=2）
                    for msg in msgs:
                        if msg.message_type != 1:
                            continue
                        task: asyncio.Task[None] = asyncio.create_task(
                            self._handle_message(http_client, ilink, msg),  # type: ignore[union-attr]
                            name=f"weixin_msg_{msg.message_id}",
                        )
                        self._pending_tasks.add(task)
                        task.add_done_callback(self._pending_tasks.discard)

        logger.info("WeChat bridge 主循环退出")

    # ----------------------------------------------------------------
    # 单条消息处理
    # ----------------------------------------------------------------

    async def _handle_message(
        self, http_client: httpx.AsyncClient, ilink: ILinkClient, msg: WeixinMessage
    ) -> None:
        """
        处理单条用户消息：（可选）下载媒体 → 调用 adapter.reply() → 发送回复。

        复用主循环的 httpx.AsyncClient 连接池，避免每条消息建立新 TCP 连接。
        """
        async with self._semaphore:
            # 自动下载媒体
            if self._auto_download_media and msg.media_items:
                await self._download_media_items(http_client, ilink, msg)

            has_text = bool(msg.text)
            has_downloaded_media = any(
                item.media_bytes is not None for item in msg.media_items
            )

            if not has_text and not has_downloaded_media:
                if msg.has_media and not self._auto_download_media:
                    logger.debug(
                        "WeChat bridge: 跳过媒体消息（auto_download_media=False）| from=%s",
                        msg.from_user_id,
                    )
                else:
                    logger.debug(
                        "WeChat bridge: 跳过空消息 | from=%s",
                        msg.from_user_id,
                    )
                return

            logger.info(
                "WeChat bridge: 收到消息 | from=%s | text=%.80r | media=%d",
                msg.from_user_id,
                msg.text,
                len(msg.media_items),
            )

            session_key = f"{WEIXIN_SESSION_PREFIX}{msg.from_user_id}"
            existing = await self._storage.get(session_key)
            if existing is None:
                await self._adapter.on_new_session(msg.from_user_id)
            await self._storage.set(session_key, "1", ttl=self._session_ttl)

            try:
                raw_reply = await self._adapter.reply(msg)
                reply = _normalize_reply(raw_reply)

                if not reply.items:
                    logger.warning(
                        "WeChat bridge: adapter 返回空回复 | from=%s", msg.from_user_id
                    )
                    return

                await self._send_reply(http_client, ilink, msg, reply)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "WeChat bridge: 消息处理异常 | from=%s | error=%s",
                    msg.from_user_id,
                    e,
                    exc_info=True,
                )

    async def _download_media_items(
        self,
        http_client: httpx.AsyncClient,
        ilink: ILinkClient,
        msg: WeixinMessage,
    ) -> None:
        """为消息中所有带 CDN 字段的媒体 item 下载内容到 item.media_bytes"""
        for item in msg.items:
            if item.type not in (2, 4, 5):
                continue
            if not item.encrypt_query_param or not item.aes_key:
                continue
            try:
                item.media_bytes = await ilink.download_media(
                    http_client, item.encrypt_query_param, item.aes_key
                )
                logger.debug(
                    "WeChat bridge: 媒体下载完成 | type=%d | size=%d | from=%s",
                    item.type, len(item.media_bytes), msg.from_user_id,
                )
            except Exception as e:
                logger.warning(
                    "WeChat bridge: 媒体下载失败 | type=%d | from=%s | error=%s",
                    item.type, msg.from_user_id, e,
                )

    async def _send_reply(
        self,
        http_client: httpx.AsyncClient,
        ilink: ILinkClient,
        msg: WeixinMessage,
        reply: Reply,
    ) -> None:
        """按序发送 Reply 中的每个 ReplyItem"""
        for item in reply.items:
            if isinstance(item, TextReplyItem):
                text = item.text
                if not text or not text.strip():
                    continue
                ok = await ilink.sendmessage(
                    client=http_client,
                    to_user_id=msg.from_user_id,
                    context_token=msg.context_token,
                    text=text,
                )
                if ok:
                    logger.info(
                        "WeChat bridge: 文本回复已发送 | to=%s | len=%d",
                        msg.from_user_id, len(text),
                    )
                else:
                    logger.warning(
                        "WeChat bridge: sendmessage ret 非预期值 | to=%s（消息可能已投递）",
                        msg.from_user_id,
                    )

            elif isinstance(item, MediaReplyItem):
                media_type_int = MEDIA_TYPE_MAP.get(item.media_type, 4)
                try:
                    item_dict = await upload_media(
                        ilink, http_client, item.data, media_type_int, filename=item.filename
                    )
                    ok = await ilink.sendmessage_items(
                        client=http_client,
                        to_user_id=msg.from_user_id,
                        context_token=msg.context_token,
                        item_list=[item_dict],
                    )
                    if ok:
                        logger.info(
                            "WeChat bridge: 媒体回复已发送 | to=%s | type=%s | size=%d",
                            msg.from_user_id, item.media_type, len(item.data),
                        )
                    else:
                        logger.warning(
                            "WeChat bridge: 媒体 sendmessage ret 非预期值 | to=%s",
                            msg.from_user_id,
                        )
                except Exception as e:
                    logger.error(
                        "WeChat bridge: 媒体回复发送失败 | to=%s | type=%s | error=%s",
                        msg.from_user_id, item.media_type, e,
                        exc_info=True,
                    )
