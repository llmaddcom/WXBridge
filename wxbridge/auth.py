"""
微信 iLink Bot 鉴权管理

职责：持久化 bot_token / bot_id / base_url 到存储后端，管理扫码登录流程。

设计说明：
  - token 存储在后端（默认 Redis），服务重启后自动恢复，无需重新扫码
  - 登录流程是阻塞式的（poll_login 会等待用户扫码确认）
  - 二维码有效期约 5 分钟，过期后最多自动重试 3 次
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .ilink_client import ILinkClient, ILINK_BASE_URL
from .storage import (
    Storage,
    WEIXIN_BOT_TOKEN,
    WEIXIN_BOT_ID,
    WEIXIN_BASE_URL,
    WEIXIN_LOGIN_QR_TOKEN,
    WEIXIN_LOGIN_QR_IMG,
    WEIXIN_LOGIN_STATUS,
)

logger = logging.getLogger(__name__)

_QR_TTL = 300       # 二维码在存储中缓存 5 分钟
_QR_MAX_RETRY = 3   # 二维码过期后最多重新申请次数


class WeixinAuth:
    """
    iLink Bot 鉴权管理

    token 存储在可插拔后端，重启后自动恢复。
    登录流程（poll_login）会阻塞直到扫码完成或超时。
    """

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    async def load_token(self) -> tuple[str, str, str] | None:
        """
        从存储加载已持久化的 token

        Returns:
            (bot_token, bot_id, base_url)，未登录返回 None
        """
        token = await self._storage.get(WEIXIN_BOT_TOKEN)
        if not token:
            return None
        bot_id = await self._storage.get(WEIXIN_BOT_ID) or ""
        base_url = await self._storage.get(WEIXIN_BASE_URL) or ILINK_BASE_URL
        return token, bot_id, base_url

    async def save_token(self, bot_token: str, bot_id: str, base_url: str) -> None:
        """将登录成功的 token 持久化（不设 TTL，由微信服务控制有效期）"""
        await self._storage.set(WEIXIN_BOT_TOKEN, bot_token)
        await self._storage.set(WEIXIN_BOT_ID, bot_id)
        await self._storage.set(WEIXIN_BASE_URL, base_url or ILINK_BASE_URL)
        logger.info("WeChat bot_token 已保存 | bot_id=%s", bot_id)

    async def clear_token(self) -> None:
        """清除已存储的 token（登出）"""
        await self._storage.delete(WEIXIN_BOT_TOKEN, WEIXIN_BOT_ID, WEIXIN_BASE_URL)
        logger.info("WeChat bot_token 已清除")

    async def start_login(self) -> tuple[str, str]:
        """
        发起登录第一步：向 iLink 申请登录二维码

        二维码 token 和图片内容暂存到存储（TTL=5min）。

        Returns:
            (qrcode_token, qrcode_img_content)
        """
        async with httpx.AsyncClient() as client:
            tmp = ILinkClient(bot_token="", base_url=ILINK_BASE_URL)
            qrcode_token, img = await tmp.get_qrcode(client)

        await self._storage.set(WEIXIN_LOGIN_QR_TOKEN, qrcode_token, ttl=_QR_TTL)
        await self._storage.set(WEIXIN_LOGIN_QR_IMG, img, ttl=_QR_TTL)
        await self._storage.set(
            WEIXIN_LOGIN_STATUS, "pending", ttl=_QR_TTL * _QR_MAX_RETRY
        )
        logger.info("WeChat 登录二维码已生成，等待用户扫码...")
        return qrcode_token, img

    async def poll_login(self) -> str:
        """
        等待扫码确认（阻塞，直到成功/失败）

        二维码过期时自动重新申请（最多 _QR_MAX_RETRY 次）。

        Returns:
            "confirmed" — 登录成功，token 已写入存储
            "expired"   — 二维码多次过期，放弃
            "error"     — 网络异常或存储中无二维码
        """
        for attempt in range(_QR_MAX_RETRY):
            qrcode_token = await self._storage.get(WEIXIN_LOGIN_QR_TOKEN)
            if not qrcode_token:
                logger.warning("WeChat poll_login: 存储中无二维码，请先调用 start_login")
                return "error"

            async with httpx.AsyncClient() as client:
                tmp = ILinkClient(bot_token="", base_url=ILINK_BASE_URL)
                while True:
                    try:
                        data = await tmp.poll_qrcode_status(client, qrcode_token)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning("WeChat 登录轮询异常: %s", e)
                        await self._storage.set(WEIXIN_LOGIN_STATUS, "failed")
                        return "error"

                    status = data.get("status", "wait")

                    if status == "confirmed":
                        await self.save_token(
                            bot_token=data["bot_token"],
                            bot_id=data.get("ilink_bot_id", ""),
                            base_url=data.get("baseurl") or ILINK_BASE_URL,
                        )
                        await self._storage.set(WEIXIN_LOGIN_STATUS, "confirmed")
                        return "confirmed"

                    elif status == "expired":
                        logger.info(
                            "WeChat 二维码已过期，重新申请（第 %d/%d 次）",
                            attempt + 1,
                            _QR_MAX_RETRY,
                        )
                        qrcode_token, _ = await self.start_login()
                        break  # 跳出内层循环用新 token 继续

                    elif status in ("wait", "scaned"):
                        await asyncio.sleep(2)

                    else:
                        logger.warning("WeChat 登录未知状态: %s", status)
                        await self._storage.set(WEIXIN_LOGIN_STATUS, "failed")
                        return "error"

        await self._storage.set(WEIXIN_LOGIN_STATUS, "failed")
        return "expired"

    async def get_pending_qrcode_img(self) -> str | None:
        """获取当前待扫描的二维码图片内容（供管理接口展示）"""
        return await self._storage.get(WEIXIN_LOGIN_QR_IMG)

    async def get_login_status(self) -> str:
        """获取当前登录流程状态（pending / confirmed / failed / none）"""
        status = await self._storage.get(WEIXIN_LOGIN_STATUS)
        return status or "none"
