"""
WXBridge 数据模型

定义微信消息相关的 Pydantic 数据模型。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class MessageItem(BaseModel):
    """消息条目，对应 item_list 中的单个元素"""

    type: int  # 1=TEXT, 2=IMAGE, 3=VOICE(STT), 4=FILE, 5=VIDEO
    text: str | None = None  # TEXT 或语音 STT 转写时有值


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
            elif t == 3:
                # 语音消息：取 STT 转写文字
                text = (item.get("voice_item") or {}).get("text")
            items.append(MessageItem(type=t, text=text))

        result.append(
            WeixinMessage(
                seq=m.get("seq", 0),
                message_id=m.get("message_id", ""),
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
