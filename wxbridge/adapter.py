"""
WXBridge AI 适配器接口

开发者只需继承 AIAdapter 并实现 reply() 方法，即可接入 WXBridge。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from .models import AdapterReply, Reply, WeixinMessage


class AIAdapter(ABC):
    """
    AI 适配器抽象基类。

    继承此类并实现 reply() 方法：

        class MyAdapter(AIAdapter):
            async def reply(self, message: WeixinMessage) -> str:
                # 返回 str（向后兼容）
                return await my_ai.chat(message.from_user_id, message.text)

        class MediaAdapter(AIAdapter):
            async def reply(self, message: WeixinMessage) -> Reply:
                # 返回 Reply（支持媒体）
                if message.media_items and message.media_items[0].media_bytes:
                    return Reply.image(message.media_items[0].media_bytes)
                return Reply.text(f"你说：{message.text}")
    """

    @abstractmethod
    async def reply(self, message: WeixinMessage) -> AdapterReply:
        """
        处理一条微信消息并返回回复。

        Args:
            message: 包含用户消息内容的 WeixinMessage 对象
                - message.text           用户文本（或语音转写结果）
                - message.from_user_id   稳定的 iLink UID（用作用户唯一标识）
                - message.session_id     iLink 会话 ID（可选上下文）
                - message.media_items    媒体条目列表（auto_download_media=True 时含 media_bytes）
                - message.context_token  由桥接层处理，适配器无需关心

        Returns:
            str:   纯文本回复（向后兼容）
            Reply: 结构化回复，支持文本和媒体的任意组合
        """
        ...

    async def on_new_session(self, from_user_id: str) -> None:
        """
        会话超时后新会话开始时调用（默认 no-op）。

        当用户空闲超过 session_ttl 秒后再次发消息时触发。
        可覆写以重置对话历史、日志记录等。
        """


@runtime_checkable
class AIAdapterProtocol(Protocol):
    """AIAdapter 的 Protocol 版本，支持鸭子类型（无需显式继承）"""

    async def reply(self, message: WeixinMessage) -> AdapterReply: ...


__all__ = ["AIAdapter", "AIAdapterProtocol", "Reply", "AdapterReply"]
