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
import os
import random
import uuid
from typing import Any

import httpx

from .models import WeixinMessage, parse_messages_from_raw

logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.0.1"

# 媒体类型映射（字符串 → sendmessage item_list type 整数）
MEDIA_TYPE_MAP: dict[str, int] = {
    "image": 2,
    "voice": 3,
    "file": 4,
    "video": 5,
}

# sendmessage MessageItemType → getuploadurl UploadMediaType 映射
# MessageItemType: 2=image, 3=voice, 4=file, 5=video
# UploadMediaType: 1=image, 2=video, 3=file, 4=voice（来自官方 SDK types.ts）
_MSG_TYPE_TO_UPLOAD_TYPE: dict[int, int] = {
    2: 1,  # image
    3: 4,  # voice
    4: 3,  # file
    5: 2,  # video
}

# 超时配置（秒）
_TIMEOUT_GETUPDATES = 40   # 服务端长轮询 35s，客户端多留 5s 余量
_TIMEOUT_SEND = 15
_TIMEOUT_QR_POLL = 40
_TIMEOUT_DEFAULT = 15
_TIMEOUT_CDN = 60          # CDN 上传/下载可能较慢
_TIMEOUT_TYPING = 10

# 单条文本消息长度上限（分段发送时每段不超过此字符数）
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

_CDN_UPLOAD_MAX_RETRIES = 3


async def upload_to_cdn(
    client: httpx.AsyncClient,
    encrypted_data: bytes,
    upload_full_url: str = "",
    upload_param: str = "",
    filekey: str = "",
) -> str:
    """
    将 AES 加密后的媒体内容 POST 上传到 CDN，失败时自动重试（最多 3 次）。

    Args:
        client:           httpx 客户端
        encrypted_data:   AES-128-ECB 加密后的字节
        upload_full_url:  getuploadurl 响应中的 upload_full_url（预签名直链，优先使用）
        upload_param:     getuploadurl 响应中的 upload_param（fallback）
        filekey:          本次上传的 filekey（upload_param 路径时必填）

    Returns:
        CDN POST 响应头 x-encrypted-param（用作出站消息的 encrypt_query_param）
    """
    import urllib.parse
    from .media import CDN_BASE_URL

    trimmed_full = upload_full_url.strip() if upload_full_url else ""
    if trimmed_full:
        cdn_url = trimmed_full
    elif upload_param:
        cdn_url = (
            f"{CDN_BASE_URL}/upload"
            f"?encrypted_query_param={urllib.parse.quote(upload_param)}"
            f"&filekey={urllib.parse.quote(filekey)}"
        )
    else:
        raise ValueError("upload_to_cdn: 需要 upload_full_url 或 upload_param")

    last_error: Exception | None = None
    for attempt in range(1, _CDN_UPLOAD_MAX_RETRIES + 1):
        try:
            resp = await client.post(
                cdn_url,
                content=encrypted_data,
                headers={"Content-Type": "application/octet-stream"},
                timeout=_TIMEOUT_CDN,
            )
            if 400 <= resp.status_code < 500:
                err_msg = resp.headers.get("x-error-message") or resp.text
                raise ILinkHTTPError(resp.status_code, f"CDN 上传客户端错误 {resp.status_code}: {err_msg}")
            _check_response(resp)
            download_param = resp.headers.get("x-encrypted-param", "")
            if not download_param:
                raise ValueError("CDN 上传响应缺少 x-encrypted-param 头")
            return download_param
        except ILinkHTTPError:
            raise  # 4xx 不重试，直接上抛
        except Exception as e:
            last_error = e
            if attempt < _CDN_UPLOAD_MAX_RETRIES:
                logger.warning("CDN 上传失败（第 %d 次），重试... error=%s", attempt, e)
            else:
                logger.error("CDN 上传全部 %d 次尝试均失败 error=%s", _CDN_UPLOAD_MAX_RETRIES, e)

    raise last_error or ValueError("CDN 上传失败")


async def upload_media(
    ilink: "ILinkClient",
    client: httpx.AsyncClient,
    data: bytes,
    media_type_int: int,
    filename: str = "",
    to_user_id: str = "",
) -> dict[str, Any]:
    """
    完整媒体上传流程：生成密钥 → AES 加密 → getuploadurl → POST CDN。

    Args:
        ilink:          ILinkClient 实例（用于调用 getuploadurl）
        client:         httpx 客户端
        data:           原始（未加密）媒体字节
        media_type_int: sendmessage item type（2=图片, 3=语音, 4=文件, 5=视频）
        filename:       文件名（type=4 必填，其他可选）
        to_user_id:     目标用户 iLink UID（getuploadurl 必填）

    Returns:
        可直接放入 sendmessage_items item_list 的字典，例如：
        {"type": 2, "image_item": {"media": {"encrypt_query_param": "...", "aes_key": "..."}}}
    """
    from .media import (
        aes_encrypt,
        aes_key_to_b64,
        aes_key_to_hex,
        generate_aes_key,
        md5_bytes,
        _require_cryptography,
    )
    _require_cryptography()

    key = generate_aes_key()
    aes_key_b64 = aes_key_to_b64(key)    # sendmessage media.aes_key 格式
    aes_key_hex = aes_key_to_hex(key)    # getuploadurl aeskey 格式（官方 SDK）
    raw_md5 = md5_bytes(data)
    raw_size = len(data)

    encrypted = aes_encrypt(data, key)
    enc_size = len(encrypted)

    # sendmessage MessageItemType → getuploadurl UploadMediaType 转换
    upload_type = _MSG_TYPE_TO_UPLOAD_TYPE.get(media_type_int, 3)  # 默认 FILE=3

    # 官方 SDK 使用 randomBytes(16).toString("hex") 作为 filekey（32 字符 hex）
    filekey = os.urandom(16).hex()
    upload_info = await ilink.getuploadurl(
        client,
        filekey=filekey,
        media_type=upload_type,
        to_user_id=to_user_id,
        raw_size=raw_size,
        raw_md5=raw_md5,
        encrypted_size=enc_size,
        aes_key_hex=aes_key_hex,
        no_need_thumb=True,
    )

    # 官方 SDK 优先使用 upload_full_url（预签名直链），fallback 到 upload_param
    upload_full_url = (upload_info.get("upload_full_url") or "").strip()
    upload_param_str = upload_info.get("upload_param") or ""
    if not upload_full_url and not upload_param_str:
        raise ValueError(f"getuploadurl 未返回可用上传 URL，完整响应：{upload_info}")

    # 从 CDN POST 响应头取 encrypt_query_param（官方 SDK cdn-upload.ts）
    encrypt_query_param = await upload_to_cdn(
        client,
        encrypted_data=encrypted,
        upload_full_url=upload_full_url,
        upload_param=upload_param_str,
        filekey=filekey,
    )
    if not encrypt_query_param:
        raise ValueError("CDN 上传响应缺少 x-encrypted-param 头")

    # 构造 item 字典（media 子对象结构与入站一致）
    _type_to_field = {2: "image_item", 3: "voice_item", 4: "file_item", 5: "video_item"}
    field_name = _type_to_field.get(media_type_int, "file_item")

    media_payload: dict[str, Any] = {
        "encrypt_query_param": encrypt_query_param,
        "aes_key": aes_key_b64,
        "encrypt_type": 1,
    }
    item_inner: dict[str, Any] = {"media": media_payload}
    if media_type_int == 2:
        # image_item: mid_size = 密文大小（官方 SDK send.ts sendImageMessageWeixin）
        item_inner["mid_size"] = enc_size
    elif media_type_int == 5:
        # video_item: video_size = 密文大小（官方 SDK send.ts sendVideoMessageWeixin）
        item_inner["video_size"] = enc_size
    elif media_type_int == 4:
        # file_item: file_name + len 明文大小字符串（官方 SDK send.ts sendFileMessageWeixin）
        if filename:
            item_inner["file_name"] = filename
        item_inner["len"] = str(raw_size)

    return {"type": media_type_int, field_name: item_inner}


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
        suppress_empty_token_warn: bool = False,
    ) -> bool:
        """
        通用 sendmessage，支持任意 item_list（文本、图片、文件、视频等）。

        这是低级接口；发送文本消息请使用 sendmessage()。

        Args:
            item_list: 符合 iLink API 格式的条目列表
            suppress_empty_token_warn: 调用方故意传空 context_token 时设为 True，
                                       避免产生误导性 WARNING（如任务汇报推送场景）。

        Returns:
            True=成功，False=接口返回错误
        """
        if not context_token and not suppress_empty_token_warn:
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

        超长文本自动分段发送（每段最多 _MAX_MESSAGE_LENGTH 字），不截断。
        context_token 必须从入站消息原样回传，否则微信无法关联对话。

        Returns:
            True=全部分段成功，False=任一分段返回错误
        """
        chunks = [text[i:i + _MAX_MESSAGE_LENGTH] for i in range(0, len(text), _MAX_MESSAGE_LENGTH)]
        if not chunks:
            chunks = [text]

        ok = True
        for chunk in chunks:
            result = await self.sendmessage_items(
                client,
                to_user_id=to_user_id,
                context_token=context_token,
                item_list=[{"type": 1, "text_item": {"text": chunk}}],
                client_id=client_id,
            )
            if not result:
                ok = False
        return ok

    async def download_media(
        self,
        client: httpx.AsyncClient,
        encrypt_query_param: str,
        aes_key_b64: str,
    ) -> bytes:
        """
        从 CDN 下载并解密媒体文件。

        Args:
            encrypt_query_param: 来自消息 item 的 CDN query string（base64 编码）
            aes_key_b64:         来自消息 item 的 Base64 AES 密钥

        Returns:
            解密后的原始字节
        """
        from .media import aes_decrypt, aes_key_from_b64, _require_cryptography
        _require_cryptography()

        from .media import CDN_BASE_URL
        import urllib.parse
        # 官方 SDK cdn-url.ts：/download?encrypted_query_param={url_encoded_param}
        url = f"{CDN_BASE_URL}/download?encrypted_query_param={urllib.parse.quote(encrypt_query_param)}"
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
        aes_key_hex: str,
        to_user_id: str = "",
        no_need_thumb: bool = True,
    ) -> dict[str, Any]:
        """
        申请 CDN 上传地址

        Args:
            filekey:        UUID，本次上传的唯一标识
            media_type:     UploadMediaType（1=图片, 2=视频, 3=文件, 4=语音）
            raw_size:       未加密数据大小（字节）
            raw_md5:        未加密数据的 hex MD5
            encrypted_size: 加密后数据大小（字节）
            aes_key_hex:    hex 字符串格式的 AES-128 密钥（官方 SDK cdn-upload.ts）
            to_user_id:     目标用户 iLink UID（官方 SDK 必填）
            no_need_thumb:  是否跳过缩略图上传 URL，默认 True

        Returns:
            原始 API 响应，upload_param 字段为字符串（CDN 上传 encrypted_query_param）
        """
        resp = await client.post(
            self._url("/ilink/bot/getuploadurl"),
            headers=_auth_headers(self.bot_token),
            json={
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": raw_size,
                "rawfilemd5": raw_md5,
                "filesize": encrypted_size,
                "no_need_thumb": no_need_thumb,
                "aeskey": aes_key_hex,
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
