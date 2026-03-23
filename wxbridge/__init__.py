"""
WXBridge — 通用微信 iLink Bot 接入库

让任何 AI 产品都可以通过腾讯 iLink Bot API 接入微信个人号。

Quick Start:
    from wxbridge import WeixinBridge, AIAdapter, WeixinMessage

    class MyAdapter(AIAdapter):
        async def reply(self, message: WeixinMessage) -> str:
            return f"您好，您说：{message.text}"

    import asyncio
    bridge = WeixinBridge(adapter=MyAdapter(), redis_url="redis://localhost")
    asyncio.run(bridge.start())
"""

from .adapter import AIAdapter, AIAdapterProtocol
from .auth import WeixinAuth
from .bridge import WeixinBridge
from .models import MessageItem, WeixinMessage, parse_messages_from_raw
from .storage import DictStorage, RedisStorage, Storage

__all__ = [
    # 核心接口
    "WeixinBridge",
    "AIAdapter",
    "AIAdapterProtocol",
    # 数据模型
    "WeixinMessage",
    "MessageItem",
    "parse_messages_from_raw",
    # 存储后端
    "Storage",
    "DictStorage",
    "RedisStorage",
    # 认证管理
    "WeixinAuth",
]

__version__ = "0.1.0"
