"""
测试 auth.py：token 持久化和登录状态管理
"""
import pytest

from wxbridge import DictStorage, WeixinAuth
from wxbridge.storage import (
    WEIXIN_BOT_TOKEN,
    WEIXIN_BOT_ID,
    WEIXIN_BASE_URL,
    WEIXIN_LOGIN_STATUS,
)


async def test_load_token_returns_none_when_empty(dict_storage: DictStorage) -> None:
    auth = WeixinAuth(dict_storage)
    result = await auth.load_token()
    assert result is None


async def test_save_and_load_token(dict_storage: DictStorage) -> None:
    auth = WeixinAuth(dict_storage)
    await auth.save_token("bot_tok_123", "bot_id_abc", "https://custom.base.url")

    result = await auth.load_token()
    assert result is not None
    token, bot_id, base_url = result
    assert token == "bot_tok_123"
    assert bot_id == "bot_id_abc"
    assert base_url == "https://custom.base.url"


async def test_save_token_uses_default_base_url_when_empty(dict_storage: DictStorage) -> None:
    from wxbridge.ilink_client import ILINK_BASE_URL

    auth = WeixinAuth(dict_storage)
    await auth.save_token("tok", "bot_id", "")

    result = await auth.load_token()
    assert result is not None
    _, _, base_url = result
    assert base_url == ILINK_BASE_URL


async def test_clear_token(dict_storage: DictStorage) -> None:
    auth = WeixinAuth(dict_storage)
    await auth.save_token("tok", "bot_id", "https://url")
    await auth.clear_token()

    result = await auth.load_token()
    assert result is None


async def test_get_login_status_default_none(dict_storage: DictStorage) -> None:
    auth = WeixinAuth(dict_storage)
    status = await auth.get_login_status()
    assert status == "none"


async def test_get_login_status_after_set(dict_storage: DictStorage) -> None:
    auth = WeixinAuth(dict_storage)
    await dict_storage.set(WEIXIN_LOGIN_STATUS, "pending")
    status = await auth.get_login_status()
    assert status == "pending"


async def test_get_pending_qrcode_img_none_when_no_login(dict_storage: DictStorage) -> None:
    auth = WeixinAuth(dict_storage)
    img = await auth.get_pending_qrcode_img()
    assert img is None
