"""
测试 bridge.py：消息派发、过滤、errcode=-14、游标持久化
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from wxbridge import DictStorage, WeixinBridge, WeixinMessage
from wxbridge.ilink_client import ILinkClient
from wxbridge.storage import WEIXIN_BOT_TOKEN, WEIXIN_BOT_ID, WEIXIN_BASE_URL, WEIXIN_CURSOR

from .conftest import MockAdapter, make_message


async def _setup_bridge(
    storage: DictStorage,
    mock_adapter: MockAdapter,
    response: str = "mock reply",
) -> WeixinBridge:
    """初始化带 token 的 bridge"""
    await storage.set(WEIXIN_BOT_TOKEN, "test_token")
    await storage.set(WEIXIN_BOT_ID, "bot_001")
    await storage.set(WEIXIN_BASE_URL, "https://ilinkai.weixin.qq.com")
    mock_adapter.response = response
    return WeixinBridge(adapter=mock_adapter, storage=storage)


async def test_handle_message_calls_adapter(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """用户消息应触发 adapter.reply()"""
    bridge = await _setup_bridge(dict_storage, mock_adapter)
    ilink = MagicMock(spec=ILinkClient)
    ilink.sendmessage = AsyncMock(return_value=True)

    msg = make_message(text="hello")
    await bridge._handle_message(MagicMock(spec=httpx.AsyncClient), ilink, msg)

    assert len(mock_adapter.calls) == 1
    assert mock_adapter.calls[0].text == "hello"
    ilink.sendmessage.assert_awaited_once()


async def test_handle_message_non_text_skipped(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """非文本消息（items 无 text）应被跳过，adapter 不被调用"""
    bridge = await _setup_bridge(dict_storage, mock_adapter)
    ilink = MagicMock(spec=ILinkClient)
    ilink.sendmessage = AsyncMock()

    msg = make_message(text=None)  # 无文本
    await bridge._handle_message(MagicMock(spec=httpx.AsyncClient), ilink, msg)

    assert mock_adapter.calls == []
    ilink.sendmessage.assert_not_awaited()


async def test_handle_message_empty_reply_not_sent(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """adapter 返回空字符串时，不发送消息"""
    mock_adapter.response = ""
    bridge = await _setup_bridge(dict_storage, mock_adapter, response="")
    ilink = MagicMock(spec=ILinkClient)
    ilink.sendmessage = AsyncMock()

    msg = make_message(text="ping")
    await bridge._handle_message(MagicMock(spec=httpx.AsyncClient), ilink, msg)

    ilink.sendmessage.assert_not_awaited()


async def test_bridge_filters_bot_messages(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """message_type=2（Bot 自身消息）不应触发 adapter"""
    bridge = await _setup_bridge(dict_storage, mock_adapter)

    # 模拟 getupdates 返回一条 Bot 自身消息，再返回 CancelledError 停止循环
    bot_msg = make_message(message_type=2, text="bot outgoing")
    call_count = 0

    async def mock_getupdates(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [bot_msg], "cursor_1", None
        raise asyncio.CancelledError()

    with patch.object(ILinkClient, "getupdates", side_effect=mock_getupdates):
        bridge._running = True
        try:
            await bridge._run()
        except asyncio.CancelledError:
            pass

    assert mock_adapter.calls == []


async def test_bridge_cursor_persisted(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """getupdates 返回新游标时应写入存储"""
    bridge = await _setup_bridge(dict_storage, mock_adapter)

    call_count = 0

    async def mock_getupdates(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [], "new_cursor_v2", None
        raise asyncio.CancelledError()

    with patch.object(ILinkClient, "getupdates", side_effect=mock_getupdates):
        bridge._running = True
        try:
            await bridge._run()
        except asyncio.CancelledError:
            pass

    stored_cursor = await dict_storage.get(WEIXIN_CURSOR)
    assert stored_cursor == "new_cursor_v2"


async def test_bridge_errcode_minus14_stops_bridge(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """errcode=-14 时 bridge 应停止并清除 token"""
    bridge = await _setup_bridge(dict_storage, mock_adapter)

    async def mock_getupdates(*args, **kwargs):
        return [], "", -14

    with patch.object(ILinkClient, "getupdates", side_effect=mock_getupdates):
        bridge._running = True
        await bridge._run()

    # token 应被清除
    assert await dict_storage.get(WEIXIN_BOT_TOKEN) is None
    # bridge 应已停止
    assert bridge._running is False


async def test_bridge_no_token_waits_and_retries(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """无 token 时应等待，token 写入后应继续运行"""
    bridge = WeixinBridge(adapter=mock_adapter, storage=dict_storage)

    call_count = 0

    async def mock_sleep(seconds: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            # 写入 token 并停止
            await dict_storage.set(WEIXIN_BOT_TOKEN, "tok")
            await dict_storage.set(WEIXIN_BOT_ID, "bot")
            await dict_storage.set(WEIXIN_BASE_URL, "https://ilinkai.weixin.qq.com")
            bridge._running = False

    with patch("wxbridge.bridge.asyncio.sleep", side_effect=mock_sleep):
        bridge._running = True
        await bridge._run()

    assert call_count >= 1


async def test_handle_message_exception_does_not_crash(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """消息处理中的异常不应向上传播（主循环不应崩溃）"""

    async def failing_reply(message: WeixinMessage) -> str:
        raise RuntimeError("AI service unavailable")

    mock_adapter.reply = failing_reply  # type: ignore[method-assign]
    bridge = await _setup_bridge(dict_storage, mock_adapter)

    ilink = MagicMock(spec=ILinkClient)
    ilink.sendmessage = AsyncMock()

    msg = make_message(text="trigger error")
    # _handle_message 内部捕获异常，不应 raise
    await bridge._handle_message(MagicMock(spec=httpx.AsyncClient), ilink, msg)

    ilink.sendmessage.assert_not_awaited()


async def test_bridge_is_running_property(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    bridge = WeixinBridge(adapter=mock_adapter, storage=dict_storage)
    assert bridge.is_running is False

    await dict_storage.set(WEIXIN_BOT_TOKEN, "tok")
    await dict_storage.set(WEIXIN_BOT_ID, "bot")
    await dict_storage.set(WEIXIN_BASE_URL, "https://ilinkai.weixin.qq.com")

    # Mock _run to immediately return
    async def noop() -> None:
        bridge._running = False

    with patch.object(bridge, "_run", side_effect=noop):
        await bridge.start()
        # After noop completes, task is done
        await asyncio.sleep(0)

    assert bridge.is_running is False
