"""
测试共享 fixtures
"""
import pytest

from wxbridge import AIAdapter, DictStorage, WeixinMessage


class MockAdapter(AIAdapter):
    """记录调用历史的 mock 适配器"""

    def __init__(self, response: str = "mock reply") -> None:
        self.response = response
        self.calls: list[WeixinMessage] = []

    async def reply(self, message: WeixinMessage) -> str:
        self.calls.append(message)
        return self.response


@pytest.fixture
def dict_storage() -> DictStorage:
    return DictStorage()


@pytest.fixture
def mock_adapter() -> MockAdapter:
    return MockAdapter()


def make_message(
    *,
    from_user_id: str = "user_001",
    message_type: int = 1,
    context_token: str = "ctx_token_001",
    text: str | None = "hello",
    message_id: str = "msg_001",
) -> WeixinMessage:
    """构建测试用 WeixinMessage"""
    from wxbridge import MessageItem

    items = []
    if text is not None:
        items.append(MessageItem(type=1, text=text))

    return WeixinMessage(
        seq=1,
        message_id=message_id,
        from_user_id=from_user_id,
        to_user_id="bot_001",
        create_time_ms=1700000000000,
        session_id="session_001",
        message_type=message_type,
        message_state=0,
        context_token=context_token,
        items=items,
    )
