"""
微信 iLink Bot 消息桥接服务

职责：维护长轮询循环，将微信用户消息路由至 AIAdapter，再把回复通过 sendmessage 发回微信。

工作流程：
  1. 加载存储中的 bot_token（未登录时等待）
  2. 循环调用 getupdates（长轮询）
  3. 收到用户消息（message_type=1）→ asyncio.create_task(_handle_message)
  4. _handle_message：调用 adapter.reply()，将回复通过 sendmessage 发送

异常处理：
  - errcode=-14（session 过期）：清除 token，停止 bridge，等待重新登录
  - 连续 3 次失败：退避 30s 再重试
  - 单条消息处理异常：记录日志，不影响主循环
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from .adapter import AIAdapter
from .auth import WeixinAuth
from .ilink_client import ILinkClient, ILINK_BASE_URL
from .models import WeixinMessage
from .storage import Storage, RedisStorage, WEIXIN_CURSOR

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class WeixinBridge:
    """
    微信 iLink Bot 消息桥接

    每条用户消息作为独立 asyncio.Task 并发处理，互不阻塞。

    Usage:
        bridge = WeixinBridge(adapter=MyAdapter(), redis_url="redis://localhost")
        await bridge.start()
    """

    def __init__(
        self,
        adapter: AIAdapter,
        storage: Storage | None = None,
        redis_url: str = "redis://localhost",
        session_ttl: int = 3600,
    ) -> None:
        """
        Args:
            adapter:     AI 适配器实例（实现了 reply() 方法）
            storage:     存储后端；None 时自动创建 RedisStorage(redis_url)
            redis_url:   Redis 连接 URL（storage=None 时有效）
            session_ttl: 会话空闲 TTL（秒），超时后下一条消息开新会话（默认 1 小时）
        """
        self._adapter = adapter
        self._storage: Storage = storage if storage is not None else RedisStorage(redis_url)
        self._session_ttl = session_ttl
        self._auth = WeixinAuth(self._storage)
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def auth(self) -> WeixinAuth:
        """暴露认证管理器，供调用方发起登录流程"""
        return self._auth

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
        """优雅停止桥接（取消后台任务并等待退出）"""
        self._running = False
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
          3. 对每条用户消息派发独立 Task
          4. 异常时指数退避，errcode=-14 时清除 token 并停止
        """
        logger.info("WeChat bridge 主循环启动")
        consecutive_errors = 0

        async with httpx.AsyncClient() as http_client:
            while self._running:
                # 加载 token（每轮都重新加载，支持 token 刷新）
                token_info = await self._auth.load_token()
                if not token_info:
                    logger.warning(
                        "WeChat bridge: 尚未登录微信，10s 后重试（请先调用 auth.start_login()）"
                    )
                    await asyncio.sleep(10)
                    continue

                bot_token, bot_id, base_url = token_info
                ilink = ILinkClient(bot_token=bot_token, base_url=base_url)
                cursor = await self._storage.get(WEIXIN_CURSOR) or ""

                try:
                    msgs, new_cursor, errcode = await ilink.getupdates(http_client, cursor)
                except asyncio.CancelledError:
                    raise
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
                user_msgs = [m for m in msgs if m.message_type == 1]
                for msg in user_msgs:
                    asyncio.create_task(
                        self._handle_message(ilink, msg),
                        name=f"weixin_msg_{msg.message_id}",
                    )

        logger.info("WeChat bridge 主循环退出")

    # ----------------------------------------------------------------
    # 单条消息处理
    # ----------------------------------------------------------------

    async def _handle_message(self, ilink: ILinkClient, msg: WeixinMessage) -> None:
        """
        处理单条用户消息：调用 adapter.reply()，通过 sendmessage 回复用户。

        非文本消息（图片/文件/视频）直接跳过。
        """
        text = msg.text
        if not text:
            logger.debug(
                "WeChat bridge: 跳过非文本消息 | from=%s types=%s",
                msg.from_user_id,
                [i.type for i in msg.items],
            )
            return

        logger.info(
            "WeChat bridge: 收到消息 | from=%s | text=%.80r",
            msg.from_user_id,
            text,
        )

        try:
            reply = await self._adapter.reply(msg)
            if not reply:
                logger.warning(
                    "WeChat bridge: adapter 返回空回复 | from=%s", msg.from_user_id
                )
                return

            async with httpx.AsyncClient() as http_client:
                ok = await ilink.sendmessage(
                    client=http_client,
                    to_user_id=msg.from_user_id,
                    context_token=msg.context_token,
                    text=reply,
                )

            if ok:
                logger.info(
                    "WeChat bridge: 回复已发送 | to=%s | len=%d",
                    msg.from_user_id,
                    len(reply),
                )
            else:
                logger.warning(
                    "WeChat bridge: sendmessage ret 非预期值 | to=%s（消息可能已投递）",
                    msg.from_user_id,
                )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "WeChat bridge: 消息处理异常 | from=%s | error=%s",
                msg.from_user_id,
                e,
                exc_info=True,
            )
