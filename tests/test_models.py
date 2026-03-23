"""
测试 models.py：WeixinMessage, MessageItem, parse_messages_from_raw
"""
import pytest

from wxbridge import MessageItem, WeixinMessage, parse_messages_from_raw


def test_weixin_message_text_from_text_item() -> None:
    msg = WeixinMessage(
        message_type=1,
        context_token="tok",
        items=[MessageItem(type=1, text="hello world")],
    )
    assert msg.text == "hello world"


def test_weixin_message_text_from_voice_stt() -> None:
    msg = WeixinMessage(
        message_type=1,
        context_token="tok",
        items=[MessageItem(type=3, text="语音转文字结果")],
    )
    assert msg.text == "语音转文字结果"


def test_weixin_message_text_returns_first_nonempty() -> None:
    msg = WeixinMessage(
        message_type=1,
        context_token="tok",
        items=[
            MessageItem(type=2, text=None),  # image, no text
            MessageItem(type=1, text="first text"),
            MessageItem(type=1, text="second text"),
        ],
    )
    assert msg.text == "first text"


def test_weixin_message_text_none_when_no_items() -> None:
    msg = WeixinMessage(message_type=1, context_token="tok")
    assert msg.text is None


def test_weixin_message_text_none_for_image_only() -> None:
    msg = WeixinMessage(
        message_type=1,
        context_token="tok",
        items=[MessageItem(type=2, text=None)],
    )
    assert msg.text is None


def test_parse_messages_from_raw_text() -> None:
    raw = [
        {
            "seq": 10,
            "message_id": "abc",
            "from_user_id": "user1",
            "to_user_id": "bot1",
            "create_time_ms": 1700000000000,
            "session_id": "sess1",
            "message_type": 1,
            "message_state": 0,
            "context_token": "ctx1",
            "item_list": [
                {"type": 1, "text_item": {"text": "你好"}},
            ],
        }
    ]
    msgs = parse_messages_from_raw(raw)
    assert len(msgs) == 1
    assert msgs[0].text == "你好"
    assert msgs[0].from_user_id == "user1"
    assert msgs[0].message_type == 1


def test_parse_messages_from_raw_voice_stt() -> None:
    raw = [
        {
            "seq": 1,
            "message_id": "v1",
            "from_user_id": "u1",
            "to_user_id": "b1",
            "create_time_ms": 0,
            "session_id": "s1",
            "message_type": 1,
            "message_state": 0,
            "context_token": "c1",
            "item_list": [
                {"type": 3, "voice_item": {"text": "语音识别结果"}},
            ],
        }
    ]
    msgs = parse_messages_from_raw(raw)
    assert msgs[0].text == "语音识别结果"
    assert msgs[0].items[0].type == 3


def test_parse_messages_from_raw_image_no_text() -> None:
    raw = [
        {
            "seq": 1,
            "message_id": "i1",
            "from_user_id": "u1",
            "to_user_id": "b1",
            "create_time_ms": 0,
            "session_id": "s1",
            "message_type": 1,
            "message_state": 0,
            "context_token": "c1",
            "item_list": [
                {"type": 2},  # image item, no text
            ],
        }
    ]
    msgs = parse_messages_from_raw(raw)
    assert msgs[0].text is None
    assert msgs[0].items[0].type == 2


def test_parse_messages_from_raw_empty() -> None:
    assert parse_messages_from_raw([]) == []


def test_parse_messages_from_raw_missing_fields() -> None:
    """缺少字段时应使用默认值，不抛异常"""
    msgs = parse_messages_from_raw([{}])
    assert len(msgs) == 1
    assert msgs[0].from_user_id == ""
    assert msgs[0].text is None
