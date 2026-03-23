"""
测试 storage.py：DictStorage CRUD 和 TTL 行为
"""
import asyncio
import time

import pytest

from wxbridge import DictStorage


async def test_get_set_basic(dict_storage: DictStorage) -> None:
    await dict_storage.set("key1", "value1")
    result = await dict_storage.get("key1")
    assert result == "value1"


async def test_get_missing_key(dict_storage: DictStorage) -> None:
    result = await dict_storage.get("nonexistent")
    assert result is None


async def test_delete_single_key(dict_storage: DictStorage) -> None:
    await dict_storage.set("k1", "v1")
    await dict_storage.delete("k1")
    assert await dict_storage.get("k1") is None


async def test_delete_multiple_keys(dict_storage: DictStorage) -> None:
    await dict_storage.set("k1", "v1")
    await dict_storage.set("k2", "v2")
    await dict_storage.delete("k1", "k2")
    assert await dict_storage.get("k1") is None
    assert await dict_storage.get("k2") is None


async def test_delete_nonexistent_key_no_error(dict_storage: DictStorage) -> None:
    # 删除不存在的 key 不应报错
    await dict_storage.delete("ghost_key")


async def test_set_overwrite(dict_storage: DictStorage) -> None:
    await dict_storage.set("k", "old")
    await dict_storage.set("k", "new")
    assert await dict_storage.get("k") == "new"


async def test_set_with_ttl_not_expired(dict_storage: DictStorage) -> None:
    await dict_storage.set("k", "v", ttl=3600)
    assert await dict_storage.get("k") == "v"


async def test_set_with_ttl_expired(dict_storage: DictStorage) -> None:
    """通过直接修改内部状态模拟 TTL 过期"""
    await dict_storage.set("k", "v", ttl=1)
    # 手动将过期时间设为过去
    dict_storage._store["k"] = ("v", time.monotonic() - 1)
    assert await dict_storage.get("k") is None


async def test_expire_extends_ttl(dict_storage: DictStorage) -> None:
    await dict_storage.set("k", "v", ttl=1)
    await dict_storage.expire("k", 3600)
    assert await dict_storage.get("k") == "v"


async def test_expire_nonexistent_key_no_error(dict_storage: DictStorage) -> None:
    # 对不存在的 key 调用 expire 不应报错
    await dict_storage.expire("ghost", 60)


async def test_set_no_ttl_persists(dict_storage: DictStorage) -> None:
    """无 TTL 的 key 不应过期"""
    await dict_storage.set("k", "v")
    entry = dict_storage._store["k"]
    assert entry[1] is None  # expire_at 应为 None
