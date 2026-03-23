"""
WXBridge AI 适配器接口

开发者只需继承 AIAdapter 并实现 reply() 方法，即可接入 WXBridge。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from .models import WeixinMessage


class AIAdapter(ABC):
    """
    AI 适配器抽象基类。

    继承此类并实现 reply() 方法：

        class MyAdapter(AIAdapter):
            async def reply(self, message: WeixinMessage) -> str:
                return await my_ai.chat(message.from_user_id, message.text)
    """

    @abstractmethod
    async def reply(self, message: WeixinMessage) -> str:
        """
        处理一条微信消息并返回回复文本。

        Args:
            message: 包含用户消息内容的 WeixinMessage 对象
                - message.text          用户文本（或语音转写结果）
                - message.from_user_id  稳定的 iLink UID（用作用户唯一标识）
                - message.session_id    iLink 会话 ID（可选上下文）
                - message.context_token 必须由桥接层回传，无需适配器关心

        Returns:
            回复文本字符串（不能为空）
        """
        ...


@runtime_checkable
class AIAdapterProtocol(Protocol):
    """AIAdapter 的 Protocol 版本，支持鸭子类型（无需显式继承）"""

    async def reply(self, message: WeixinMessage) -> str: ...
