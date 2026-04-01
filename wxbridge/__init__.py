"""
WXBridge — 通用微信 iLink Bot 接入库

让任何 AI 产品都可以通过腾讯 iLink Bot API 接入微信个人号。

Quick Start（文本适配器）:
    from wxbridge import WeixinBridge, AIAdapter, WeixinMessage

    class MyAdapter(AIAdapter):
        async def reply(self, message: WeixinMessage) -> str:
            return f"您好，您说：{message.text}"

    import asyncio
    bridge = WeixinBridge(adapter=MyAdapter(), redis_url="redis://localhost")
    asyncio.run(bridge.start())

媒体支持（需 pip install 'wxbridge[media]'）:
    from wxbridge import WeixinBridge, AIAdapter, WeixinMessage, Reply

    class MediaAdapter(AIAdapter):
        async def reply(self, message: WeixinMessage) -> Reply:
            if message.media_items and message.media_items[0].media_bytes:
                return Reply.image(message.media_items[0].media_bytes)
            return Reply.text(f"你说：{message.text}")

    bridge = WeixinBridge(
        adapter=MediaAdapter(),
        redis_url="redis://localhost",
        auto_download_media=True,
    )
"""

import logging as _logging

from .adapter import AIAdapter, AIAdapterProtocol
from .auth import WeixinAuth
from .bridge import WeixinBridge
from .ilink_client import ILinkHTTPError
from .models import (
    AdapterReply,
    MediaReplyItem,
    MessageItem,
    Reply,
    ReplyItem,
    TextReplyItem,
    WeixinMessage,
    parse_messages_from_raw,
)
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
    # 回复类型（适配器返回值）
    "AdapterReply",
    "Reply",
    "TextReplyItem",
    "MediaReplyItem",
    "ReplyItem",
    # 存储后端
    "Storage",
    "DictStorage",
    "RedisStorage",
    # 认证管理
    "WeixinAuth",
    # HTTP 错误
    "ILinkHTTPError",
    # 日志配置
    "configure_logging",
]


def configure_logging(level: int = _logging.INFO) -> None:
    """配置 WXBridge 默认日志格式，程序启动时调用一次"""
    _logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

__version__ = "0.5.0"
