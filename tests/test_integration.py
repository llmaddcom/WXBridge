"""
集成测试：HTTP 4xx/5xx 错误处理、并发上限、errcode=-14、空白回复过滤

全部使用 DictStorage + AsyncMock/patch，无需真实 Redis 或网络连接。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from wxbridge import DictStorage, WeixinBridge
from wxbridge.ilink_client import ILinkClient, ILinkHTTPError
from wxbridge.storage import WEIXIN_BOT_TOKEN, WEIXIN_BOT_ID, WEIXIN_BASE_URL

from .conftest import MockAdapter, make_message


async def _setup_bridge(
    storage: DictStorage,
    adapter: MockAdapter,
    response: str = "ok",
) -> WeixinBridge:
    await storage.set(WEIXIN_BOT_TOKEN, "test_token")
    await storage.set(WEIXIN_BOT_ID, "bot_001")
    await storage.set(WEIXIN_BASE_URL, "https://ilinkai.weixin.qq.com")
    adapter.response = response
    return WeixinBridge(adapter=adapter, storage=storage)


# ----------------------------------------------------------------
# 场景 A — HTTP 429 触发长退避，bridge 继续运行
# ----------------------------------------------------------------

async def test_http_429_triggers_long_backoff(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """HTTP 429 应退避 60s，bridge 不停止"""
    bridge = await _setup_bridge(dict_storage, mock_adapter)

    call_count = 0

    async def mock_getupdates(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ILinkHTTPError(429, "rate limited")
        raise asyncio.CancelledError()

    sleep_calls: list[float] = []

    async def mock_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with (
        patch.object(ILinkClient, "getupdates", side_effect=mock_getupdates),
        patch("wxbridge.bridge.asyncio.sleep", side_effect=mock_sleep),
    ):
        bridge._running = True
        try:
            await bridge._run()
        except asyncio.CancelledError:
            pass

    assert 60 in sleep_calls, f"应有 60s 退避，实际 sleep 调用: {sleep_calls}"
    # 429 不应停止 bridge（在 CancelledError 之前仍在运行）
    assert call_count == 2


# ----------------------------------------------------------------
# 场景 B — HTTP 401 停止 bridge 并清除 token
# ----------------------------------------------------------------

async def test_http_401_stops_bridge_and_clears_token(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """HTTP 401 应停止 bridge 并清除 token"""
    bridge = await _setup_bridge(dict_storage, mock_adapter)

    async def mock_getupdates(*args, **kwargs):
        raise ILinkHTTPError(401, "unauthorized")

    with patch.object(ILinkClient, "getupdates", side_effect=mock_getupdates):
        bridge._running = True
        await bridge._run()

    assert bridge._running is False
    assert await dict_storage.get(WEIXIN_BOT_TOKEN) is None


# ----------------------------------------------------------------
# 场景 C — 并发上限：同时最多 max_concurrent_tasks 个任务
# ----------------------------------------------------------------

async def test_concurrency_limit_respected(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """同时执行的 _handle_message 不超过 max_concurrent_tasks"""
    MAX = 3
    bridge = await _setup_bridge(dict_storage, mock_adapter)
    bridge._semaphore = asyncio.Semaphore(MAX)

    concurrent_count = 0
    max_seen = 0
    gate = asyncio.Event()

    async def slow_reply(message):
        nonlocal concurrent_count, max_seen
        concurrent_count += 1
        max_seen = max(max_seen, concurrent_count)
        await gate.wait()
        concurrent_count -= 1
        return "reply"

    mock_adapter.reply = slow_reply  # type: ignore[method-assign]

    ilink = MagicMock(spec=ILinkClient)
    ilink.sendmessage = AsyncMock(return_value=True)

    msgs = [make_message(text=f"msg{i}", message_id=f"id{i}") for i in range(10)]

    # 派发全部任务，让它们卡在 gate
    tasks = [
        asyncio.create_task(bridge._handle_message(MagicMock(spec=httpx.AsyncClient), ilink, m))
        for m in msgs
    ]

    # 让事件循环调度，信号量会阻塞超出上限的任务
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert max_seen <= MAX, f"最大并发 {max_seen} 超过上限 {MAX}"

    gate.set()
    await asyncio.gather(*tasks, return_exceptions=True)


# ----------------------------------------------------------------
# 场景 D — errcode=-14 mid-run 停止 bridge
# ----------------------------------------------------------------

async def test_errcode_minus14_stops_bridge(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """getupdates 返回 errcode=-14 时 bridge 应在本次循环内停止"""
    bridge = await _setup_bridge(dict_storage, mock_adapter)

    call_count = 0

    async def mock_getupdates(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [], "", None  # 第一次正常
        return [], "", -14      # 第二次触发 session 过期

    with patch.object(ILinkClient, "getupdates", side_effect=mock_getupdates):
        bridge._running = True
        await bridge._run()

    assert bridge._running is False
    assert await dict_storage.get(WEIXIN_BOT_TOKEN) is None
    assert call_count == 2


# ----------------------------------------------------------------
# 场景 E — 空白回复不发送
# ----------------------------------------------------------------

async def test_whitespace_reply_not_sent(
    dict_storage: DictStorage, mock_adapter: MockAdapter
) -> None:
    """adapter 返回纯空白字符串时，sendmessage 不应被调用"""
    bridge = await _setup_bridge(dict_storage, mock_adapter, response="\n\t  ")
    ilink = MagicMock(spec=ILinkClient)
    ilink.sendmessage = AsyncMock()

    msg = make_message(text="ping")
    await bridge._handle_message(MagicMock(spec=httpx.AsyncClient), ilink, msg)

    ilink.sendmessage.assert_not_awaited()
