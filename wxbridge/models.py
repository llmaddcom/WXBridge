"""
WXBridge 数据模型

定义微信消息相关的 Pydantic 数据模型，以及适配器回复类型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field


class MessageItem(BaseModel):
    """消息条目，对应 item_list 中的单个元素"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    type: int  # 1=TEXT, 2=IMAGE, 3=VOICE(STT), 4=FILE, 5=VIDEO
    text: str | None = None  # TEXT 或语音 STT 转写时有值

    # 媒体 CDN 引用字段（来自 API 响应，type=2/4/5 时填充）
    encrypt_query_param: str | None = None  # CDN 下载 query string
    aes_key: str | None = None              # Base64 编码的 AES-128 密钥

    # 文件元数据（type=4 时填充）
    filename: str | None = None
    filesize: int | None = None

    # 由 bridge 下载后填充，不参与序列化
    media_bytes: bytes | None = Field(default=None, exclude=True)


class WeixinMessage(BaseModel):
    """
    来自 getupdates 的单条微信消息。

    message_type: 1=用户发送, 2=Bot 自身发出（需跳过）
    context_token: 必须原样回传给 sendmessage，否则关联失败
    """

    seq: int = 0
    message_id: str = ""
    from_user_id: str = ""
    to_user_id: str = ""
    create_time_ms: int = 0
    session_id: str = ""
    message_type: int = 0  # 1=USER, 2=BOT
    message_state: int = 0
    context_token: str = ""
    items: list[MessageItem] = Field(default_factory=list)

    @property
    def text(self) -> str | None:
        """提取可读文本（TEXT 或语音 STT 转写），返回第一个非空文本"""
        for item in self.items:
            if item.text:
                return item.text
        return None

    @property
    def has_media(self) -> bool:
        """消息含图片/文件/视频时为 True，用于区分媒体消息与空文本消息"""
        return any(item.type in (2, 4, 5) for item in self.items)

    @property
    def media_items(self) -> list[MessageItem]:
        """返回所有媒体条目（图片/文件/视频），不含语音（语音已 STT 转文字）"""
        return [item for item in self.items if item.type in (2, 4, 5)]


# ----------------------------------------------------------------
# 适配器回复类型
# ----------------------------------------------------------------

@dataclass
class TextReplyItem:
    """文本回复条目"""
    text: str


@dataclass
class MediaReplyItem:
    """
    媒体回复条目。调用方提供原始字节，bridge 负责加密上传。

    media_type: "image" | "voice" | "file" | "video"
    filename:   文件类型必填，其他类型可选
    """
    data: bytes
    media_type: str  # "image" | "voice" | "file" | "video"
    filename: str = ""


# Python 3.10 union 类型别名
ReplyItem = Union[TextReplyItem, MediaReplyItem]


@dataclass
class Reply:
    """
    结构化回复，可包含文本和媒体条目的任意组合。
    bridge 按顺序逐项发送，每项对应一次 sendmessage 调用。

    Example:
        # 纯文本
        Reply.text("你好")

        # 纯图片
        Reply.image(img_bytes)

        # 文本 + 图片
        Reply(items=[TextReplyItem("这是图片"), MediaReplyItem(img_bytes, "image")])
    """
    items: list[ReplyItem] = field(default_factory=list)

    @staticmethod
    def text(content: str) -> "Reply":
        """创建纯文本回复"""
        return Reply(items=[TextReplyItem(content)])

    @staticmethod
    def image(data: bytes) -> "Reply":
        """创建图片回复"""
        return Reply(items=[MediaReplyItem(data=data, media_type="image")])

    @staticmethod
    def file(data: bytes, filename: str) -> "Reply":
        """创建文件回复"""
        return Reply(items=[MediaReplyItem(data=data, media_type="file", filename=filename)])

    @staticmethod
    def video(data: bytes, filename: str = "") -> "Reply":
        """创建视频回复"""
        return Reply(items=[MediaReplyItem(data=data, media_type="video", filename=filename)])


# 适配器返回类型别名：str（向后兼容）或 Reply（结构化回复）
AdapterReply = Union[str, Reply]


def parse_messages_from_raw(raw: list[dict[str, Any]]) -> list[WeixinMessage]:
    """将 getupdates 返回的原始 JSON 列表解析为 WeixinMessage 列表"""
    result = []
    for m in raw:
        items: list[MessageItem] = []
        for item in m.get("item_list") or []:
            t = item.get("type", 0)
            text: str | None = None

            if t == 1:
                text = (item.get("text_item") or {}).get("text")
                items.append(MessageItem(type=t, text=text))

            elif t == 3:
                # 语音消息：取 STT 转写文字
                text = (item.get("voice_item") or {}).get("text")
                items.append(MessageItem(type=t, text=text))

            elif t == 2:
                # 图片
                img = item.get("image_item") or {}
                items.append(MessageItem(
                    type=t,
                    encrypt_query_param=img.get("encrypt_query_param"),
                    aes_key=img.get("aes_key"),
                ))

            elif t == 4:
                # 文件
                fi = item.get("file_item") or {}
                items.append(MessageItem(
                    type=t,
                    encrypt_query_param=fi.get("encrypt_query_param"),
                    aes_key=fi.get("aes_key"),
                    filename=fi.get("name"),
                    filesize=fi.get("rawsize"),
                ))

            elif t == 5:
                # 视频
                vi = item.get("video_item") or {}
                items.append(MessageItem(
                    type=t,
                    encrypt_query_param=vi.get("encrypt_query_param"),
                    aes_key=vi.get("aes_key"),
                ))

            else:
                items.append(MessageItem(type=t))

        result.append(
            WeixinMessage(
                seq=m.get("seq", 0),
                message_id=str(m.get("message_id", "")),
                from_user_id=m.get("from_user_id", ""),
                to_user_id=m.get("to_user_id", ""),
                create_time_ms=m.get("create_time_ms", 0),
                session_id=m.get("session_id", ""),
                message_type=m.get("message_type", 0),
                message_state=m.get("message_state", 0),
                context_token=m.get("context_token", ""),
                items=items,
            )
        )
    return result
