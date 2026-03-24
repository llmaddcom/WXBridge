"""
iLink Bot API HTTP 客户端

职责：封装腾讯 iLink Bot API 的所有 HTTP 调用（长轮询拉消息、发消息、媒体上传/下载、登录二维码）。

协议说明：
  - 基础地址：https://ilinkai.weixin.qq.com
  - CDN 地址：https://novac2c.cdn.weixin.qq.com/c2c
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

# 媒体类型映射（字符串 → iLink API 整数）
MEDIA_TYPE_MAP: dict[str, int] = {
    "image": 2,
    "voice": 3,
    "file": 4,
    "video": 5,
}

# 超时配置（秒）
_TIMEOUT_GETUPDATES = 40   # 服务端长轮询 35s，客户端多留 5s 余量
_TIMEOUT_SEND = 15
_TIMEOUT_QR_POLL = 40
_TIMEOUT_DEFAULT = 15
_TIMEOUT_CDN = 60          # CDN 上传/下载可能较慢
_TIMEOUT_TYPING = 10

# 消息长度上限（保守值，避免 WeChat API 因超长消息报错）
_MAX_MESSAGE_LENGTH = 2000


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


class ILinkHTTPError(Exception):
    """替代 httpx.HTTPStatusError，携带可操作的元数据"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable: bool = status_code >= 500 or status_code == 429
        self.rate_limited: bool = status_code == 429
        self.auth_failed: bool = status_code == 401


def _check_response(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    raise ILinkHTTPError(resp.status_code, f"iLink HTTP {resp.status_code}: {resp.url}")


# ----------------------------------------------------------------
# 模块级 CDN 辅助函数
# ----------------------------------------------------------------

async def upload_to_cdn(
    client: httpx.AsyncClient,
    upload_url: str,
    encrypted_data: bytes,
) -> None:
    """
    将 AES 加密后的媒体内容 PUT 上传到 CDN。

    Args:
        client:         httpx 客户端
        upload_url:     getuploadurl 返回的上传地址
        encrypted_data: AES-128-ECB 加密后的字节
    """
    resp = await client.put(
        upload_url,
        content=encrypted_data,
        headers={"Content-Type": "application/octet-stream"},
        timeout=_TIMEOUT_CDN,
    )
    _check_response(resp)


async def upload_media(
    ilink: "ILinkClient",
    client: httpx.AsyncClient,
    data: bytes,
    media_type_int: int,
    filename: str = "",
) -> dict[str, Any]:
    """
    完整媒体上传流程：生成密钥 → AES 加密 → getuploadurl → PUT CDN。

    Args:
        ilink:          ILinkClient 实例（用于调用 getuploadurl）
        client:         httpx 客户端
        data:           原始（未加密）媒体字节
        media_type_int: 2=图片, 3=语音, 4=文件, 5=视频
        filename:       文件名（type=4 必填，其他可选）

    Returns:
        可直接放入 sendmessage_items item_list 的字典，例如：
        {"type": 2, "image_item": {"encrypt_query_param": "...", "aes_key": "..."}}
    """
    from .media import (
        aes_encrypt,
        aes_key_to_b64,
        generate_aes_key,
        md5_bytes,
        _require_cryptography,
    )
    _require_cryptography()

    key = generate_aes_key()
    aes_key_b64 = aes_key_to_b64(key)
    raw_md5 = md5_bytes(data)
    raw_size = len(data)

    encrypted = aes_encrypt(data, key)
    enc_size = len(encrypted)

    filekey = str(uuid.uuid4())
    upload_info = await ilink.getuploadurl(
        client,
        filekey=filekey,
        media_type=media_type_int,
        raw_size=raw_size,
        raw_md5=raw_md5,
        encrypted_size=enc_size,
        aes_key_b64=aes_key_b64,
    )

    upload_param = upload_info.get("upload_param") or {}
    upload_url = upload_param.get("upload_url", "")
    if not upload_url:
        raise ValueError(f"getuploadurl 返回空 upload_url，完整响应：{upload_info}")

    await upload_to_cdn(client, upload_url, encrypted)

    encrypt_query_param = upload_param.get("encrypt_query_param", "")

    # 构造 item 字典
    _type_to_field = {2: "image_item", 3: "voice_item", 4: "file_item", 5: "video_item"}
    field_name = _type_to_field.get(media_type_int, "file_item")

    item_payload: dict[str, Any] = {
        "encrypt_query_param": encrypt_query_param,
        "aes_key": aes_key_b64,
    }
    if filename:
        item_payload["name"] = filename

    return {"type": media_type_int, field_name: item_payload}


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
        _check_response(resp)
        data = resp.json()

        errcode = data.get("errcode")
        msgs = parse_messages_from_raw(data.get("msgs") or [])
        new_cursor = data.get("get_updates_buf") or cursor
        return msgs, new_cursor, errcode

    async def sendmessage_items(
        self,
        client: httpx.AsyncClient,
        to_user_id: str,
        context_token: str,
        item_list: list[dict[str, Any]],
        client_id: str | None = None,
    ) -> bool:
        """
        通用 sendmessage，支持任意 item_list（文本、图片、文件、视频等）。

        这是低级接口；发送文本消息请使用 sendmessage()。

        Args:
            item_list: 符合 iLink API 格式的条目列表

        Returns:
            True=成功，False=接口返回错误
        """
        if not context_token:
            logger.warning("sendmessage_items: context_token 为空，消息可能无法关联")

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
                    "item_list": item_list,
                },
                "base_info": _base_info(),
            },
            timeout=_TIMEOUT_SEND,
        )
        _check_response(resp)
        data = resp.json()
        logger.debug("sendmessage response: %s", data)
        ret = data.get("ret")
        return ret == 0 or ret is None

    async def sendmessage(
        self,
        client: httpx.AsyncClient,
        to_user_id: str,
        context_token: str,
        text: str,
        client_id: str | None = None,
    ) -> bool:
        """
        发送文本消息（内部委托给 sendmessage_items）

        context_token 必须从入站消息原样回传，否则微信无法关联对话。

        Returns:
            True=发送成功，False=接口返回错误
        """
        if len(text) > _MAX_MESSAGE_LENGTH:
            logger.warning(
                "sendmessage: 消息过长（%d 字），截断至 %d 字", len(text), _MAX_MESSAGE_LENGTH
            )
            text = text[:_MAX_MESSAGE_LENGTH]

        return await self.sendmessage_items(
            client,
            to_user_id=to_user_id,
            context_token=context_token,
            item_list=[{"type": 1, "text_item": {"text": text}}],
            client_id=client_id,
        )

    async def download_media(
        self,
        client: httpx.AsyncClient,
        encrypt_query_param: str,
        aes_key_b64: str,
    ) -> bytes:
        """
        从 CDN 下载并解密媒体文件。

        Args:
            encrypt_query_param: 来自消息 item 的 CDN query string
            aes_key_b64:         来自消息 item 的 Base64 AES 密钥

        Returns:
            解密后的原始字节
        """
        from .media import aes_decrypt, aes_key_from_b64, _require_cryptography
        _require_cryptography()

        from .media import CDN_BASE_URL
        url = f"{CDN_BASE_URL}?{encrypt_query_param}"
        resp = await client.get(url, timeout=_TIMEOUT_CDN)
        _check_response(resp)

        encrypted = resp.content
        key = aes_key_from_b64(aes_key_b64)
        return aes_decrypt(encrypted, key)

    async def getconfig(
        self,
        client: httpx.AsyncClient,
        to_user_id: str,
        context_token: str,
    ) -> dict[str, Any]:
        """
        获取对话配置（含 typing_ticket，用于 sendtyping）

        Returns:
            原始 API 响应字典
        """
        resp = await client.post(
            self._url("/ilink/bot/getconfig"),
            headers=_auth_headers(self.bot_token),
            json={
                "to_user_id": to_user_id,
                "context_token": context_token,
                "base_info": _base_info(),
            },
            timeout=_TIMEOUT_DEFAULT,
        )
        _check_response(resp)
        return resp.json()  # type: ignore[no-any-return]

    async def sendtyping(
        self,
        client: httpx.AsyncClient,
        to_user_id: str,
        context_token: str,
        typing_ticket: str,
        status: int = 1,
    ) -> bool:
        """
        发送输入状态指示（"对方正在输入..."）

        Args:
            typing_ticket: 从 getconfig 获取
            status:        1=输入中, 2=取消

        Returns:
            True=成功
        """
        resp = await client.post(
            self._url("/ilink/bot/sendtyping"),
            headers=_auth_headers(self.bot_token),
            json={
                "to_user_id": to_user_id,
                "context_token": context_token,
                "typing_ticket": typing_ticket,
                "status": status,
                "base_info": _base_info(),
            },
            timeout=_TIMEOUT_TYPING,
        )
        _check_response(resp)
        data = resp.json()
        ret = data.get("ret")
        return ret == 0 or ret is None

    async def getuploadurl(
        self,
        client: httpx.AsyncClient,
        filekey: str,
        media_type: int,
        raw_size: int,
        raw_md5: str,
        encrypted_size: int,
        aes_key_b64: str,
    ) -> dict[str, Any]:
        """
        申请 CDN 上传地址

        Args:
            filekey:        UUID，本次上传的唯一标识
            media_type:     2=图片, 3=语音, 4=文件, 5=视频
            raw_size:       未加密数据大小（字节）
            raw_md5:        未加密数据的 hex MD5
            encrypted_size: 加密后数据大小（字节）
            aes_key_b64:    Base64 编码的 AES-128 密钥

        Returns:
            原始 API 响应，含 upload_param（包含 upload_url, encrypt_query_param）
        """
        resp = await client.post(
            self._url("/ilink/bot/getuploadurl"),
            headers=_auth_headers(self.bot_token),
            json={
                "filekey": filekey,
                "media_type": media_type,
                "rawsize": raw_size,
                "rawfilemd5": raw_md5,
                "filesize": encrypted_size,
                "aeskey": aes_key_b64,
                "base_info": _base_info(),
            },
            timeout=_TIMEOUT_DEFAULT,
        )
        _check_response(resp)
        return resp.json()  # type: ignore[no-any-return]

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
        return resp.json()  # type: ignore[no-any-return]
