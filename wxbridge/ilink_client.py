"""
iLink Bot API HTTP 客户端

职责：封装腾讯 iLink Bot API 的所有 HTTP 调用（长轮询拉消息、发消息、登录二维码）。

协议说明：
  - 基础地址：https://ilinkai.weixin.qq.com
  - 鉴权：Authorization: Bearer <bot_token> + AuthorizationType: ilink_bot_token
  - 所有业务接口均为 POST JSON，登录接口为 GET
  - errcode=-14 表示 session 过期，需重新登录
"""
from __future__ import annotations

import base64
import logging
import random
import uuid
from typing import Any

import httpx

from .models import WeixinMessage, parse_messages_from_raw

logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.2"

# 超时配置（秒）
_TIMEOUT_GETUPDATES = 40   # 服务端长轮询 35s，客户端多留 5s 余量
_TIMEOUT_SEND = 15
_TIMEOUT_QR_POLL = 40
_TIMEOUT_DEFAULT = 15


# ----------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------

def _make_uin_header() -> str:
    """
    生成随机 X-WECHAT-UIN header

    协议要求：base64(随机 uint32 的十进制字符串)
    """
    val = str(random.randint(0, 2**32 - 1))
    return base64.b64encode(val.encode()).decode()


def _auth_headers(bot_token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {bot_token}",
        "X-WECHAT-UIN": _make_uin_header(),
    }


def _base_info() -> dict[str, str]:
    return {"channel_version": CHANNEL_VERSION}


# ----------------------------------------------------------------
# 客户端
# ----------------------------------------------------------------

class ILinkClient:
    """
    腾讯 iLink Bot API 客户端

    持有 bot_token 和 base_url，外部注入后即可调用。
    不持有 httpx.AsyncClient，由调用方传入（便于连接复用）。
    """

    def __init__(self, bot_token: str, base_url: str = ILINK_BASE_URL) -> None:
        self.bot_token = bot_token
        self.base_url = base_url.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def getupdates(
        self,
        client: httpx.AsyncClient,
        cursor: str = "",
    ) -> tuple[list[WeixinMessage], str, int | None]:
        """
        长轮询拉取新消息

        服务端最长 35s 才返回（无新消息时阻塞），客户端超时设为 40s。

        Returns:
            (messages, new_cursor, errcode)
            errcode=-14 → session 过期，需重新登录
        """
        resp = await client.post(
            self._url("/ilink/bot/getupdates"),
            headers=_auth_headers(self.bot_token),
            json={"get_updates_buf": cursor, "base_info": _base_info()},
            timeout=_TIMEOUT_GETUPDATES,
        )
        resp.raise_for_status()
        data = resp.json()

        errcode = data.get("errcode")
        msgs = parse_messages_from_raw(data.get("msgs") or [])
        new_cursor = data.get("get_updates_buf") or cursor
        return msgs, new_cursor, errcode

    async def sendmessage(
        self,
        client: httpx.AsyncClient,
        to_user_id: str,
        context_token: str,
        text: str,
        client_id: str | None = None,
    ) -> bool:
        """
        发送文本消息

        context_token 必须从入站消息原样回传，否则微信无法关联对话。

        Returns:
            True=发送成功，False=接口返回错误
        """
        if not context_token:
            logger.warning("sendmessage: context_token 为空，消息可能无法关联")

        resp = await client.post(
            self._url("/ilink/bot/sendmessage"),
            headers=_auth_headers(self.bot_token),
            json={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id or str(uuid.uuid4()),
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": _base_info(),
            },
            timeout=_TIMEOUT_SEND,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.debug("sendmessage response: %s", data)
        # iLink API 成功时 ret=0 或字段不存在（消息已投递但无明确成功码）
        ret = data.get("ret")
        return ret == 0 or ret is None

    async def get_qrcode(
        self, client: httpx.AsyncClient
    ) -> tuple[str, str]:
        """
        申请登录二维码（无需 token）

        Returns:
            (qrcode_token, qrcode_img_content)
        """
        resp = await client.get(
            self._url("/ilink/bot/get_bot_qrcode"),
            params={"bot_type": "3"},
            timeout=_TIMEOUT_DEFAULT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["qrcode"], data.get("qrcode_img_content", "")

    async def poll_qrcode_status(
        self, client: httpx.AsyncClient, qrcode_token: str
    ) -> dict[str, Any]:
        """
        轮询扫码状态（长轮询，约 35s 才返回）

        Returns 原始响应字典：
          status: "wait" | "scaned" | "confirmed" | "expired"
          confirmed 时含 bot_token, ilink_bot_id, baseurl
        """
        resp = await client.get(
            self._url("/ilink/bot/get_qrcode_status"),
            params={"qrcode": qrcode_token},
            headers={"iLink-App-ClientVersion": "1"},
            timeout=_TIMEOUT_QR_POLL,
        )
        resp.raise_for_status()
        return resp.json()
