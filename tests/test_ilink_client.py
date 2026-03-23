"""
测试 ilink_client.py：使用 mock httpx 响应测试 HTTP 层
"""
import json

import httpx
import pytest

from wxbridge.ilink_client import ILinkClient, ILINK_BASE_URL


def _make_transport(responses: list[dict]) -> httpx.MockTransport:
    """创建按顺序返回响应的 mock transport"""
    index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal index
        resp = responses[index % len(responses)]
        index += 1
        return httpx.Response(
            status_code=resp.get("status_code", 200),
            json=resp.get("json", {}),
        )

    return httpx.MockTransport(handler)


async def test_getupdates_happy_path() -> None:
    raw_response = {
        "errcode": None,
        "get_updates_buf": "cursor_v2",
        "msgs": [
            {
                "seq": 1,
                "message_id": "m1",
                "from_user_id": "user1",
                "to_user_id": "bot1",
                "create_time_ms": 1000,
                "session_id": "s1",
                "message_type": 1,
                "message_state": 0,
                "context_token": "ctx1",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            }
        ],
    }

    transport = _make_transport([{"json": raw_response}])
    async with httpx.AsyncClient(transport=transport) as client:
        ilink = ILinkClient(bot_token="test_token")
        msgs, cursor, errcode = await ilink.getupdates(client, "cursor_v1")

    assert len(msgs) == 1
    assert msgs[0].text == "hello"
    assert cursor == "cursor_v2"
    assert errcode is None


async def test_getupdates_errcode_minus14() -> None:
    raw_response = {"errcode": -14, "get_updates_buf": "", "msgs": []}

    transport = _make_transport([{"json": raw_response}])
    async with httpx.AsyncClient(transport=transport) as client:
        ilink = ILinkClient(bot_token="expired_token")
        msgs, cursor, errcode = await ilink.getupdates(client, "")

    assert errcode == -14
    assert msgs == []


async def test_getupdates_empty_msgs() -> None:
    raw_response = {"get_updates_buf": "cursor_new", "msgs": []}

    transport = _make_transport([{"json": raw_response}])
    async with httpx.AsyncClient(transport=transport) as client:
        ilink = ILinkClient(bot_token="tok")
        msgs, cursor, errcode = await ilink.getupdates(client, "cursor_old")

    assert msgs == []
    assert cursor == "cursor_new"


async def test_getupdates_cursor_fallback() -> None:
    """当响应中无 get_updates_buf 时，保持原游标"""
    transport = _make_transport([{"json": {"msgs": []}}])
    async with httpx.AsyncClient(transport=transport) as client:
        ilink = ILinkClient(bot_token="tok")
        _, cursor, _ = await ilink.getupdates(client, "original_cursor")

    assert cursor == "original_cursor"


async def test_sendmessage_success_ret_zero() -> None:
    transport = _make_transport([{"json": {"ret": 0}}])
    async with httpx.AsyncClient(transport=transport) as client:
        ilink = ILinkClient(bot_token="tok")
        ok = await ilink.sendmessage(client, "user1", "ctx1", "hello")
    assert ok is True


async def test_sendmessage_success_ret_none() -> None:
    transport = _make_transport([{"json": {}}])
    async with httpx.AsyncClient(transport=transport) as client:
        ilink = ILinkClient(bot_token="tok")
        ok = await ilink.sendmessage(client, "user1", "ctx1", "hello")
    assert ok is True


async def test_sendmessage_failure_ret_nonzero() -> None:
    transport = _make_transport([{"json": {"ret": 1}}])
    async with httpx.AsyncClient(transport=transport) as client:
        ilink = ILinkClient(bot_token="tok")
        ok = await ilink.sendmessage(client, "user1", "ctx1", "hello")
    assert ok is False


async def test_get_qrcode() -> None:
    transport = _make_transport(
        [{"json": {"qrcode": "qr_token_123", "qrcode_img_content": "data:image/png;base64,..."}}]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        ilink = ILinkClient(bot_token="")
        qr_token, img = await ilink.get_qrcode(client)

    assert qr_token == "qr_token_123"
    assert img.startswith("data:image")


async def test_poll_qrcode_status_confirmed() -> None:
    transport = _make_transport(
        [
            {
                "json": {
                    "status": "confirmed",
                    "bot_token": "new_token",
                    "ilink_bot_id": "bot_id_001",
                    "baseurl": "https://ilinkai.weixin.qq.com",
                }
            }
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        ilink = ILinkClient(bot_token="")
        data = await ilink.poll_qrcode_status(client, "qr_tok")

    assert data["status"] == "confirmed"
    assert data["bot_token"] == "new_token"
